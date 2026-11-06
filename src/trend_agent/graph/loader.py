"""Neo4j 적재. 문서: docs/schemas/graph_schema.md §2~§4, docs/spec.md Step 5

- 입력: 정답 라벨 JSON(`{"label_meta", "extraction"}`) → Style·Item·Article, 정규화된 Product(`ProductRecord` JSONL).
- **다시 실행해도 같은 그래프**가 나온다: 노드는 `MERGE`, Product는 관계를 지우고 다시 만들고, 기사는 그 기사의 Style·Item을 지우고 한 트랜잭션에서 다시 쓴다.
- `category_major`가 없는 Item·Product(지원 밖)는 적재하지 않는다 (spec §8.2).
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
from trend_agent.schemas.graph_schema import (
    ItemRecord,
    ProductRecord,
    _Record,
    compute_style_profile,
    flatten,
    item_record_from_extraction,
    style_node_from_extraction,
)

LABELS = ["Style", "Item", "Product", "Brand", "Article", "Category", "Color", "Material", "Pattern", "Detail"]
CONSTRAINTS = [f"CREATE CONSTRAINT {l.lower()}_id IF NOT EXISTS FOR (n:{l}) REQUIRE n.id IS UNIQUE" for l in LABELS]
INDEXES = [
    "CREATE INDEX product_minor IF NOT EXISTS FOR (p:Product) ON (p.category_minor)",
    "CREATE INDEX product_fit IF NOT EXISTS FOR (p:Product) ON (p.fit)",
    "CREATE INDEX product_source IF NOT EXISTS FOR (p:Product) ON (p.source)",
    "CREATE INDEX item_article IF NOT EXISTS FOR (i:Item) ON (i.article_id)",
]
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


def ensure_schema(driver: Driver) -> None:
    for q in CONSTRAINTS + INDEXES:
        driver.execute_query(q)


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


def load_products(driver: Driver, records: Iterable[ProductRecord], batch_size: int = 500) -> dict[str, int]:
    """상분류가 있는 Product만 적재한다. 반환: {"loaded", "unique_ids", "skipped_out_of_scope"}.
    `unique_ids`가 `loaded`보다 작으면 id가 겹쳐 합쳐진 것이다 (정규화 단계에서 id를 유일하게 만들어야 한다).
    """
    loaded = skipped = 0
    seen: set[str] = set()
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
        seen.add(rec.node.id)
        if len(batch) >= batch_size:
            flush()
    flush()
    return {"loaded": loaded, "unique_ids": len(seen), "skipped_out_of_scope": skipped}


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


def load_label(driver: Driver, doc: dict[str, Any]) -> dict[str, int]:
    """라벨 JSON 하나(기사 하나)를 적재한다. 같은 기사를 다시 적재하면 그 기사의 Style·Item을 새로 쓴다."""
    meta = doc["label_meta"]
    ext = ArticleExtraction.model_validate(doc["extraction"])
    aid = ext.article_id
    article = {"id": aid, "title": meta.get("title"), "url": meta.get("url"),
               "published_at": meta.get("published"), "label_status": meta.get("status")}
    styles: list[dict[str, Any]] = []
    loaded = skipped = 0
    for n, style in enumerate(ext.styles):
        sid = f"{aid}-s{n}"
        records: list[ItemRecord] = []
        item_evidence: list[dict[str, Any]] = []
        for m, it in enumerate(style.items):
            if it.category_major is None:  # 지원 밖 Item은 적재하지 않는다
                skipped += 1
                continue
            rec = mark_item_approx(item_record_from_extraction(it, f"{sid}-i{m}", aid))
            records.append(rec)
            item_evidence += [{"item_id": rec.node.id, "block_idx": e.block_idx, "quote": e.quote} for e in it.evidence]
        node = style_node_from_extraction(style, sid)
        node = node.model_copy(update=compute_style_profile(records))
        styles.append({"props": flatten(node), "items": [record_row(r) for r in records],
                       "evidence": _style_evidence_rows(style), "item_evidence": item_evidence})
        loaded += len(records)
    with driver.session() as session:
        session.execute_write(_write_article, article, styles)
    return {"styles": len(styles), "items_loaded": loaded, "items_skipped_out_of_scope": skipped}


def load_labels(driver: Driver, label_dir: Path | str, article_ids: Optional[list[str]] = None) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for path in sorted(Path(label_dir).glob("*.json")):
        if article_ids and path.stem not in article_ids:
            continue
        out[path.stem] = load_label(driver, json.loads(path.read_text(encoding="utf-8")))
    return out


def graph_counts(driver: Driver) -> dict[str, int]:
    rows = driver.execute_query("MATCH (n) UNWIND labels(n) AS l RETURN l AS label, count(*) AS c ORDER BY label").records
    counts = {r["label"]: r["c"] for r in rows}
    rels = driver.execute_query("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t").records
    counts.update({f"-[{r['t']}]->": r["c"] for r in rels})
    return counts
