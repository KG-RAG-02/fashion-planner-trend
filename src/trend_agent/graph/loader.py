"""Neo4j 적재. 문서: docs/schemas/graph_schema.md §2~§4, docs/spec.md Step 5

- 입력: 정답 라벨 JSON(`{"label_meta", "extraction"}`) → Style·Item·Article, 정규화된 Product(`ProductRecord` JSONL).
- **다시 실행해도 같은 그래프**가 나온다: 노드는 `MERGE`, Product는 관계를 지우고 다시 만들고, 기사는 그 기사의 Style·Item을 지우고 한 트랜잭션에서 다시 쓴다.
- `category_major`가 없는 Item·Product(지원 밖)는 적재하지 않는다 (spec §8.2).

v1.1 (계약 G1):
- 기사 입력 = 라벨(`label_meta.status` silver|gold) **또는** T1 자동 추출 파일(같은 형식, status `auto`). 같은 기사는
  라벨 우선(`prefer="labels"`). Article에 `source`(label|extraction)·`labeler`·`prompt_version`·`extracted_at`.
- Item `image_urls`: 크롤 기사 `images[]`의 `{image_idx: url}` 맵으로 채운다(D4: `ImageLink.image_idx` = 크롤러 순번).
  맵에 없는 idx는 버리고 `image_links_unmapped`로 센다.
- 자동 추출(auto)은 색·소재·패턴·디테일 raw를 AliasMap으로 다시 판정해 근사(approx) 매핑이면 관계 `approx=true`
  (라벨은 라벨러 값 유지 → 기준선 불변). 근사 값은 1차 점수·후보에 쓰지 않는다.
- Term 계층은 적재마다 다시 만든다(`graph/terms.py`). 제약 `term_id`, 범위 인덱스 `style_name_norm`,
  fulltext `style_names`·`term_texts`·`product_names`(분석기 `cjk`가 있으면 cjk).
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from neo4j import Driver

from trend_agent.ingestion.normalization.alias_map import AliasMap
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.article_extraction import ArticleExtraction
from trend_agent.schemas.graph_schema import (  # noqa: I001
    ItemRecord,
    ProductRecord,
    _Record,
    compute_style_profile,
    flatten,
    item_record_from_extraction,
    style_node_from_extraction,
)

LABELS = ["Style", "Item", "Product", "Brand", "Article", "Category", "Color", "Material", "Pattern", "Detail", "Term"]
CONSTRAINTS = [f"CREATE CONSTRAINT {l.lower()}_id IF NOT EXISTS FOR (n:{l}) REQUIRE n.id IS UNIQUE" for l in LABELS]
INDEXES = [
    "CREATE INDEX product_minor IF NOT EXISTS FOR (p:Product) ON (p.category_minor)",
    "CREATE INDEX product_fit IF NOT EXISTS FOR (p:Product) ON (p.fit)",
    "CREATE INDEX product_source IF NOT EXISTS FOR (p:Product) ON (p.source)",
    "CREATE INDEX item_article IF NOT EXISTS FOR (i:Item) ON (i.article_id)",
    "CREATE INDEX style_name_norm IF NOT EXISTS FOR (s:Style) ON (s.name_norm)",  # v1.1 Tier1 정확 일치
]
# v1.1 fulltext (후보·제안용 — Tier1 판정은 name_norm 정확 일치)
FULLTEXT = [("style_names", "Style", ["name", "aliases"]), ("term_texts", "Term", ["text"]), ("product_names", "Product", ["name"])]
FALLBACK_ANALYZER = "standard-no-stop-words"
BRAND = "Musinsa"
_ALIAS: Optional[AliasMap] = None


def mark_item_approx(rec: ItemRecord) -> ItemRecord:
    """Item의 핏이 근사 매핑(슬림→타이트)이면 `approx`에 "fit"을 넣는다. 근사 값은 1차 검색에서 쓰지 않는다 (alias_map §9).
    색·소재 등 관계의 `approx`는 라벨러가 정한 값을 그대로 쓴다 (사용자가 확정한 매핑, 예: 병아리색→옐로우).
    """
    global _ALIAS
    if rec.node.fit_raw and rec.node.fit is not None:
        _ALIAS = _ALIAS or AliasMap()
        n = _ALIAS.normalize("fit", rec.node.fit_raw)
        if n.approx and n.value == _s(rec.node.fit) and "fit" not in rec.node.approx:
            rec.node.approx.append("fit")
    return rec


def _s(v: Any) -> Any:
    return v.value if isinstance(v, Enum) else v


def fulltext_analyzer(driver: Driver) -> str:
    names = {r["analyzer"] for r in driver.execute_query(
        "CALL db.index.fulltext.listAvailableAnalyzers() YIELD analyzer RETURN analyzer").records}
    return "cjk" if "cjk" in names else FALLBACK_ANALYZER


def ensure_schema(driver: Driver) -> dict[str, str]:
    """제약·인덱스·fulltext. 반환: {"fulltext_analyzer": 쓴 분석기} (적재 출력에 기록한다)."""
    for q in CONSTRAINTS + INDEXES:
        driver.execute_query(q)
    analyzer = fulltext_analyzer(driver)
    for name, label, props in FULLTEXT:
        on = ", ".join(f"n.{x}" for x in props)
        driver.execute_query(
            f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS FOR (n:{label}) ON EACH [{on}] "
            f"OPTIONS {{indexConfig: {{`fulltext.analyzer`: '{analyzer}'}}}}")
    return {"fulltext_analyzer": analyzer}


def _alias() -> AliasMap:
    global _ALIAS
    _ALIAS = _ALIAS or AliasMap()
    return _ALIAS


def mark_auto_approx(rec: ItemRecord) -> int:
    """자동 추출 Item: 색·소재·패턴·디테일 raw를 AliasMap으로 다시 판정해, 같은 값이 근사 매핑으로만 나오면 approx=true.

    T1 추출 스키마에는 관계별 approx 필드가 없어(`review_flags` 문자열뿐) 적재기가 판정한다. 반환: 표시한 관계 수.
    """
    alias = _alias()
    n = 0
    groups = [("color", e) for e in rec.colors] + [("material", e) for e in rec.materials] + \
             [("pattern", e) for e in rec.patterns] + [(_s(e.type), e) for e in rec.details]
    for attr, e in groups:
        if e.approx or not e.raw:
            continue
        same = [x for x in alias.normalize_tokens(attr, e.raw) if x.value == e.value]
        if same and all(x.approx for x in same):
            e.approx = True
            n += 1
    return n


def seed_vocabulary(driver: Driver) -> dict[str, int]:
    """표준 값 노드를 미리 만든다 (Category 4, Color 21, Material 26, Pattern 19, Detail 62, Brand 1)."""
    groups: dict[str, list[dict[str, Any]]] = {
        "Category": [{"id": m.value, "name": m.value} for m in V.CategoryMajor],
        "Color": [{"id": m.value, "name": m.value} for m in V.Color],
        "Material": [{"id": m.value, "name": m.value} for m in V.Material],
        "Pattern": [{"id": m.value, "name": m.value} for m in V.Pattern],
        "Detail": [{"id": V.detail_node_id(t, m.value), "name": m.value, "type": t.value}
                   for t, enum in V.DETAIL_ENUMS.items() for m in enum],
        "Brand": [{"id": BRAND, "name": BRAND}],
    }
    for label, rows in groups.items():
        driver.execute_query(f"UNWIND $rows AS r MERGE (n:{label} {{id: r.id}}) SET n += r", rows=rows)
    return {k: len(v) for k, v in groups.items()}


# ---------- 노드 + 관계 묶음 → UNWIND용 행 ----------
def _edge(e: Any) -> dict[str, Any]:
    return {"value": e.value, "raw": e.raw, "approx": bool(e.approx)}


def record_row(rec: _Record) -> dict[str, Any]:
    return {
        "props": flatten(rec.node),
        "major": _s(rec.category_major),
        "colors": [_edge(e) for e in rec.colors],
        "materials": [_edge(e) for e in rec.materials],
        "patterns": [_edge(e) for e in rec.patterns],
        "details": [
            {"id": V.detail_node_id(e.type, e.value), "type": _s(e.type), "name": e.value, "raw": e.raw,
             "motif": e.motif, "placement": e.placement, "approx": bool(e.approx)}
            for e in rec.details
        ],
    }


def _record_cypher(label: str, with_brand: bool) -> str:
    brand = f"MERGE (b:Brand {{id: '{BRAND}'}}) MERGE (n)-[:BELONGS_TO]->(b)" if with_brand else ""
    return f"""
UNWIND $rows AS row
MERGE (n:{label} {{id: row.props.id}})
SET n = row.props
WITH n, row
OPTIONAL MATCH (n)-[old:IN_CATEGORY|HAS_COLOR|HAS_MATERIAL|HAS_PATTERN|HAS_DETAIL|BELONGS_TO]->()
DELETE old
WITH DISTINCT n, row
MERGE (c:Category {{id: row.major}})
MERGE (n)-[:IN_CATEGORY]->(c)
{brand}
WITH n, row
CALL (n, row) {{ UNWIND row.colors AS e MERGE (a:Color {{id: e.value}}) MERGE (n)-[r:HAS_COLOR]->(a) SET r.raw = e.raw, r.approx = e.approx }}
CALL (n, row) {{ UNWIND row.materials AS e MERGE (a:Material {{id: e.value}}) MERGE (n)-[r:HAS_MATERIAL]->(a) SET r.raw = e.raw, r.approx = e.approx }}
CALL (n, row) {{ UNWIND row.patterns AS e MERGE (a:Pattern {{id: e.value}}) MERGE (n)-[r:HAS_PATTERN]->(a) SET r.raw = e.raw, r.approx = e.approx }}
CALL (n, row) {{ UNWIND row.details AS e
       MERGE (a:Detail {{id: e.id}}) ON CREATE SET a.name = e.name, a.type = e.type
       MERGE (n)-[r:HAS_DETAIL]->(a) SET r.raw = e.raw, r.motif = e.motif, r.placement = e.placement, r.approx = e.approx }}
"""


PRODUCT_CYPHER = _record_cypher("Product", with_brand=True)
ITEM_CYPHER = _record_cypher("Item", with_brand=False)


# ---------- Product ----------
def read_products(path: Path | str) -> Iterable[ProductRecord]:
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield ProductRecord.model_validate_json(line)


def load_products(driver: Driver, records: Iterable[ProductRecord], batch_size: int = 500,
                  seen: Optional[set[str]] = None) -> dict[str, int]:
    """상분류가 있는 Product만 적재한다. 반환: {"loaded", "unique_ids", "skipped_out_of_scope"}.
    `unique_ids`가 `loaded`보다 작으면 id가 겹쳐 합쳐진 것이다 (정규화 단계에서 id를 유일하게 만들어야 한다).
    `seen`을 주면 적재한 id를 더한다(prune_products용).
    """
    loaded = skipped = 0
    ids: set[str] = set()
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal loaded
        if batch:
            driver.execute_query(PRODUCT_CYPHER, rows=batch)
            loaded += len(batch)
            batch.clear()

    for rec in records:
        if not rec.in_graph_scope:
            skipped += 1
            continue
        batch.append(record_row(rec))
        ids.add(rec.node.id)
        if len(batch) >= batch_size:
            flush()
    flush()
    if seen is not None:
        seen |= ids
    return {"loaded": loaded, "unique_ids": len(ids), "skipped_out_of_scope": skipped}


# ---------- 기사 라벨 (Style · Item · Article) ----------
def _style_evidence_rows(style: Any) -> list[dict[str, Any]]:
    """`idx`는 description일 때 그 설명의 순번 (어느 설명의 근거인지 알기 위함)."""
    rows = [{"block_idx": e.block_idx, "quote": e.quote, "about": "style", "idx": None} for e in style.evidence]
    for n, d in enumerate(style.description):
        rows += [{"block_idx": e.block_idx, "quote": e.quote, "about": "description", "idx": n} for e in d.evidence]
    for f in style.focus_attributes:
        rows += [{"block_idx": e.block_idx, "quote": e.quote, "about": "focus", "idx": None} for e in f.evidence]
    return rows


def _write_article(tx: Any, article: dict[str, Any], styles: list[dict[str, Any]]) -> None:
    aid = article["id"]
    tx.run("MATCH (i:Item {article_id: $aid}) DETACH DELETE i", aid=aid)
    tx.run("MATCH (s:Style) WHERE s.id STARTS WITH $prefix DETACH DELETE s", prefix=f"{aid}-s")
    tx.run("MERGE (a:Article {id: $id}) SET a += $props", id=aid, props={k: v for k, v in article.items() if v is not None})
    for st in styles:
        tx.run("CREATE (s:Style) SET s = $props", props=st["props"])
        if st["items"]:
            tx.run(ITEM_CYPHER, rows=st["items"])
            tx.run("MATCH (s:Style {id: $sid}) UNWIND $ids AS iid MATCH (i:Item {id: iid}) MERGE (s)-[:HAS_ITEM]->(i)",
                   sid=st["props"]["id"], ids=[r["props"]["id"] for r in st["items"]])
        tx.run("MATCH (s:Style {id: $sid}), (a:Article {id: $aid}) UNWIND $rows AS r "
               "CREATE (s)-[:EVIDENCED_BY {block_idx: r.block_idx, quote: r.quote, about: r.about, idx: r.idx}]->(a)",
               sid=st["props"]["id"], aid=aid, rows=st["evidence"])
        tx.run("UNWIND $rows AS r MATCH (i:Item {id: r.item_id}), (a:Article {id: $aid}) "
               "CREATE (i)-[:EVIDENCED_BY {block_idx: r.block_idx, quote: r.quote, about: 'item'}]->(a)",
               aid=aid, rows=st["item_evidence"])


def article_props(meta: dict[str, Any], aid: str, crawl: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Article 노드 속성 (메타 어댑터). 라벨·추출 `label_meta`가 우선이고 비면 크롤 기사로 채운다."""
    crawl = crawl or {}
    status = meta.get("status")
    published = meta.get("published") or ((crawl.get("published_at") or "")[:10] or None)
    return {"id": aid, "title": meta.get("title") or crawl.get("title"), "url": meta.get("url") or crawl.get("url"),
            "published_at": published, "label_status": status,
            "source": "extraction" if status == "auto" else "label",
            "labeler": meta.get("labeler"), "prompt_version": meta.get("prompt_version"),
            "extracted_at": meta.get("extracted_at")}


def load_label(driver: Driver, doc: dict[str, Any], image_map: Optional[dict[int, str]] = None,
               crawl: Optional[dict[str, Any]] = None) -> dict[str, int]:
    """라벨(또는 같은 형식의 자동 추출) JSON 하나(기사 하나)를 적재한다. 같은 기사를 다시 적재하면 그 기사의 Style·Item을 새로 쓴다.

    image_map = 그 기사의 {크롤러 image_idx: url}. 없으면 Item 이미지는 비어 있다(v1.0 동작).
    """
    meta = doc["label_meta"]
    ext = ArticleExtraction.model_validate(doc["extraction"])
    aid = ext.article_id
    article = article_props(meta, aid, crawl)
    auto = meta.get("status") == "auto"
    styles: list[dict[str, Any]] = []
    loaded = skipped = links = unmapped = approx_marked = 0
    for n, style in enumerate(ext.styles):
        sid = f"{aid}-s{n}"
        records: list[ItemRecord] = []
        item_evidence: list[dict[str, Any]] = []
        for m, it in enumerate(style.items):
            if it.category_major is None:  # 지원 밖 Item은 적재하지 않는다
                skipped += 1
                continue
            links += len(it.image_links)
            unmapped += sum(1 for l in it.image_links if l.image_idx not in (image_map or {}))
            rec = mark_item_approx(item_record_from_extraction(it, f"{sid}-i{m}", aid, image_urls=image_map))
            if auto:
                approx_marked += mark_auto_approx(rec)
            records.append(rec)
            item_evidence += [{"item_id": rec.node.id, "block_idx": e.block_idx, "quote": e.quote} for e in it.evidence]
        node = style_node_from_extraction(style, sid, article_id=aid)
        node = node.model_copy(update=compute_style_profile(records))
        styles.append({"props": flatten(node), "items": [record_row(r) for r in records],
                       "evidence": _style_evidence_rows(style), "item_evidence": item_evidence})
        loaded += len(records)
    with driver.session() as session:
        session.execute_write(_write_article, article, styles)
    out = {"styles": len(styles), "items_loaded": loaded, "items_skipped_out_of_scope": skipped}
    if links:
        out.update(image_links=links, image_links_unmapped=unmapped)
    if auto:
        out["approx_marked"] = approx_marked
    return out


def read_crawl(path: Optional[Path | str]) -> dict[str, dict[str, Any]]:
    """크롤 JSONL → {article_id: 기사}. 경로가 없으면 빈 dict."""
    if not path or not Path(path).exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                a = json.loads(line)
                out[str(a["article_id"])] = a
    return out


def image_map_of(article: Optional[dict[str, Any]]) -> dict[int, str]:
    """크롤 기사 `images[]` → {image_idx: url} (D4)."""
    return {int(im["image_idx"]): im["url"] for im in (article or {}).get("images") or [] if im.get("url") is not None}


def article_sources(label_dir: Optional[Path | str] = None, extraction_dir: Optional[Path | str] = None,
                    prefer: str = "labels", article_ids: Optional[list[str]] = None) -> dict[str, Path]:
    """기사 id → 적재할 파일. 같은 기사가 둘 다 있으면 `prefer`(labels|extractions)를 쓴다. 하위 폴더(_review/)는 읽지 않는다."""
    found: dict[str, dict[str, Path]] = {}
    for kind, d in (("labels", label_dir), ("extractions", extraction_dir)):
        if d and Path(d).exists():
            for path in sorted(Path(d).glob("*.json")):
                found.setdefault(path.stem, {})[kind] = path
    other = "extractions" if prefer == "labels" else "labels"
    out = {}
    for aid in sorted(found):
        if article_ids and aid not in article_ids:
            continue
        out[aid] = found[aid].get(prefer) or found[aid][other]
    return out


def load_articles(driver: Driver, label_dir: Optional[Path | str] = None, extraction_dir: Optional[Path | str] = None,
                  crawl_path: Optional[Path | str] = None, prefer: str = "labels",
                  article_ids: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
    """라벨 + 자동 추출 기사를 적재한다(같은 기사는 prefer 우선). 반환: {article_id: 통계 + source 경로 종류}."""
    crawl = read_crawl(crawl_path)
    out: dict[str, dict[str, Any]] = {}
    for aid, path in article_sources(label_dir, extraction_dir, prefer, article_ids).items():
        doc = json.loads(path.read_text(encoding="utf-8"))
        stats = load_label(driver, doc, image_map=image_map_of(crawl.get(aid)) if crawl else None, crawl=crawl.get(aid))
        stats["from"] = "extraction" if doc.get("label_meta", {}).get("status") == "auto" else "label"
        out[aid] = stats
    return out


def load_labels(driver: Driver, label_dir: Path | str, article_ids: Optional[list[str]] = None,
                crawl_path: Optional[Path | str] = None) -> dict[str, dict[str, int]]:
    """v1.0 진입점 (라벨만). crawl_path를 주면 Item 이미지 맵을 채운다."""
    return load_articles(driver, label_dir=label_dir, crawl_path=crawl_path, article_ids=article_ids)


def prune_products(driver: Driver, keep_ids: set[str]) -> int:
    """이번 입력에 없는 Product를 지운다(products v2에서 id가 바뀐 경우 — 예: 세트 분해 구성품 `<원 id>#<k>`)."""
    rec = driver.execute_query("MATCH (p:Product) WHERE NOT p.id IN $ids WITH p LIMIT 100000 DETACH DELETE p RETURN count(*) AS c",
                               ids=sorted(keep_ids)).records
    return rec[0]["c"] if rec else 0


def graph_counts(driver: Driver) -> dict[str, int]:
    rows = driver.execute_query("MATCH (n) UNWIND labels(n) AS l RETURN l AS label, count(*) AS c ORDER BY label").records
    counts = {r["label"]: r["c"] for r in rows}
    rels = driver.execute_query("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t").records
    counts.update({f"-[{r['t']}]->": r["c"] for r in rels})
    return counts
