"""질의 → Style·Item 검색 → Product 후보 탐색·순위 → `TrendAnalysisResult`. 문서: docs/spec.md Step 8~10

- Style: 요청 시즌이 `Style.seasons`에 포함된 것 (시즌 미확인 Style은 보류하고 limitations에 알린다).
- Item: 카테고리 필터(상분류·하분류 include/exclude) → 요청 속성 필터 (명시적으로 다르면 제거, 미확인은 통과).
- Product 후보: **상분류가 같고** 하분류·핏·공유 속성 노드(색·소재·패턴·디테일) 중 하나가 같은 Product를 그래프에서 모으고,
  점수는 `retrieval/scoring.py`가 계산한다 (DB 없이도 같은 점수를 낼 수 있다).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from neo4j import Driver

from trend_agent.retrieval.scoring import (
    ScoredProduct,
    attribute_values,
    item_passes,
    product_passes,
    rank_products,
)
from trend_agent.retrieval.weights import WeightsConfig, load_weights
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import (
    SCORED_ATTRIBUTES,
    AttrEdge,
    DetailEdge,
    ItemNode,
    ItemRecord,
    ProductNode,
    ProductRecord,
    _Record,
)
from trend_agent.schemas.trend_analysis_result import (
    AppliedAttribute,
    AppliedFilters,
    DescriptionOut,
    DetailOut,
    EvidenceRecord,
    MatchResult,
    ProductMatch,
    Trend,
    TrendAnalysisResult,
    TrendItem,
    TrendStyle,
)
from trend_agent.schemas.trend_query import CategoryRef, TrendQuery, style_matches_season

_EDGES = """
  [(n)-[r:HAS_COLOR]->(a) | {value: a.id, raw: r.raw, approx: r.approx}] AS colors,
  [(n)-[r:HAS_MATERIAL]->(a) | {value: a.id, raw: r.raw, approx: r.approx}] AS materials,
  [(n)-[r:HAS_PATTERN]->(a) | {value: a.id, raw: r.raw, approx: r.approx}] AS patterns,
  [(n)-[r:HAS_DETAIL]->(a) | {type: a.type, value: a.name, raw: r.raw, motif: r.motif, placement: r.placement, approx: r.approx}] AS details
"""
ITEMS_CYPHER = f"""
MATCH (s:Style)-[:HAS_ITEM]->(n:Item)-[:IN_CATEGORY]->(c:Category)
WHERE s.id IN $style_ids
RETURN s.id AS sid, n{{.*}} AS props, c.id AS major, {_EDGES}
ORDER BY sid, props.id
"""
PRODUCTS_CYPHER = f"""
MATCH (n:Product)-[:IN_CATEGORY]->(c:Category)
WHERE n.id IN $ids
RETURN n{{.*}} AS props, c.id AS major, {_EDGES}
"""
_REL_ATTR = "HAS_COLOR|HAS_MATERIAL|HAS_PATTERN|HAS_DETAIL"


def _edges(rows: Iterable[dict[str, Any]]) -> list[AttrEdge]:
    return [AttrEdge(value=r["value"], raw=r.get("raw"), approx=bool(r.get("approx"))) for r in rows]


def _details(rows: Iterable[dict[str, Any]]) -> list[DetailEdge]:
    return [DetailEdge(type=r["type"], value=r["value"], raw=r.get("raw"), motif=r.get("motif"),
                       placement=r.get("placement"), approx=bool(r.get("approx"))) for r in rows]


def _record(model: type, node: type, row: dict[str, Any]) -> Any:
    return model(node=node(**row["props"]), category_major=row["major"], colors=_edges(row["colors"]),
                 materials=_edges(row["materials"]), patterns=_edges(row["patterns"]), details=_details(row["details"]))


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def _ref_matches(ref: CategoryRef, major: Optional[str], minor: Optional[str]) -> bool:
    if ref.minor is not None:
        return minor == ref.minor.value
    return ref.major is not None and major == ref.major.value


def _approx_labels(rec: _Record) -> list[str]:
    out = [f"{kind}:{e.value}" for kind, edges in (("color", rec.colors), ("material", rec.materials), ("pattern", rec.patterns))
           for e in edges if e.approx]
    out += [f"detail:{e.type.value}:{e.value}" for e in rec.details if e.approx]
    return out + [f"{n}" for n in rec.node.approx]


def attributes_out(rec: _Record) -> dict[str, Any]:
    n = rec.node
    return dict(
        category_major=rec.category_major, category_minor=n.category_minor,
        colors=[V.Color(e.value) for e in rec.colors], materials=[e.value for e in rec.materials],
        patterns=[e.value for e in rec.patterns],
        details=[DetailOut(type=e.type, name=e.value, raw=e.raw, motif=e.motif, placement=e.placement) for e in rec.details],
        fit=n.fit, shape=n.shape, length_body=n.length_body, length_sleeve=n.length_sleeve,
        length_body_raw=n.length_body_raw, length_sleeve_raw=n.length_sleeve_raw, approx=_approx_labels(rec),
    )


class TrendSearcher:
    def __init__(self, driver: Driver, weights: Optional[WeightsConfig] = None, top_k: int = 5):
        self.driver = driver
        self.weights = weights or load_weights()
        self.top_k = top_k
        self._products: dict[str, ProductRecord] = {}

    # ---------- 그래프 조회 ----------
    def _q(self, cypher: str, **params: Any) -> list[Any]:
        return self.driver.execute_query(cypher, **params).records

    def _fetch_products(self, ids: Iterable[str]) -> list[ProductRecord]:
        ids = list(ids)
        missing = [i for i in ids if i not in self._products]
        for k in range(0, len(missing), 2000):
            for r in self._q(PRODUCTS_CYPHER, ids=missing[k:k + 2000]):
                rec = _record(ProductRecord, ProductNode, r)
                self._products[rec.node.id] = rec
        return [self._products[i] for i in ids if i in self._products]

    def candidate_ids(self, item: ItemRecord, limit_when_no_attributes: Optional[int] = None) -> list[str]:
        """Item과 상분류가 같고, 하분류·핏·공유 속성 노드 중 하나가 같은 Product의 id."""
        major = item.category_major.value if hasattr(item.category_major, "value") else item.category_major
        ids: set[str] = set()
        minor = item.node.category_minor
        if minor is not None:
            minor = minor.value if hasattr(minor, "value") else minor
            order = f" ORDER BY p.id LIMIT {int(limit_when_no_attributes)}" if limit_when_no_attributes else ""
            ids |= {r["id"] for r in self._q(
                "MATCH (p:Product {category_minor: $minor})-[:IN_CATEGORY]->(:Category {id: $major}) RETURN p.id AS id" + order,
                minor=minor, major=major)}
        if limit_when_no_attributes:
            return sorted(ids)
        fit = item.usable_scalar("fit")
        if fit is not None:
            fit = fit.value if hasattr(fit, "value") else fit
            ids |= {r["id"] for r in self._q(
                "MATCH (p:Product {fit: $fit})-[:IN_CATEGORY]->(:Category {id: $major}) WHERE NOT 'fit' IN p.approx RETURN p.id AS id",
                fit=fit, major=major)}
        ids |= {r["id"] for r in self._q(
            f"MATCH (i:Item {{id: $iid}})-[ri:{_REL_ATTR}]->(a)<-[rp:{_REL_ATTR}]-(p:Product)-[:IN_CATEGORY]->(:Category {{id: $major}}) "
            "WHERE type(ri) = type(rp) AND NOT coalesce(ri.approx, false) AND NOT coalesce(rp.approx, false) RETURN DISTINCT p.id AS id",
            iid=item.node.id, major=major)}
        return sorted(ids)

    # ---------- 검색 ----------
    def search(self, query: TrendQuery) -> TrendAnalysisResult:
        limitations: list[str] = []
        season = query.trend_terms.season
        key = f"{season.year}{season.season.value}"
        applied = AppliedFilters(
            season=season, categories=query.requested.categories,
            gender=(query.requested.gender.value if query.requested.gender else None),
            attributes=[AppliedAttribute(type=a.type, value=a.value) for a in query.requested.attributes if a.value is not None])
        if season.origin == "default_next":
            limitations.append(f"시즌 미지정 → {season.year} {season.season.value}로 해석했습니다.")
        if query.requested.gender and query.requested.gender.value:
            limitations.append("성별 필터는 정책이 정해지지 않아 적용하지 않았습니다.")
        unresolved = [a.raw for a in query.requested.attributes if a.value is None]
        if unresolved:
            limitations.append(f"표준 값으로 바꾸지 못해 필터에 쓰지 않은 속성 요청: {', '.join(unresolved)}")
        cats = query.requested.categories
        if cats.include and all(r.major is None and r.minor is None for r in cats.include):
            limitations.append("요청한 카테고리가 모두 지원 범위(상의·하의·아우터·원피스) 밖입니다.")
            return TrendAnalysisResult(request_id=query.request_id, status="no_results", applied_filters=applied, limitations=limitations)

        # 1. Style (시즌, 이름)
        styles = [dict(r["s"]) for r in self._q("MATCH (s:Style) RETURN s ORDER BY s.id")]
        unknown_season = [s for s in styles if not s.get("seasons")]
        matched = [s for s in styles if style_matches_season(s.get("seasons", []), season)]
        if unknown_season:
            limitations.append(f"시즌이 확인되지 않은 트렌드 {len(unknown_season)}개는 시즌 조건 검색에서 보류했습니다.")
        terms = [_norm(t.raw) for t in query.trend_terms.style_names]
        if terms:
            matched = [s for s in matched if any(t in _norm(s["name"]) or _norm(s["name"]) in t for t in terms)]
        if not matched:
            limitations.append(f"{key}에 유행한 트렌드가 그래프에 없습니다.")
            return TrendAnalysisResult(request_id=query.request_id, status="no_results", applied_filters=applied, limitations=limitations)

        # 2. Item (카테고리 → 요청 속성)
        by_style: dict[str, list[tuple[ItemRecord, MatchResult]]] = {}
        dropped_attr = 0
        for r in self._q(ITEMS_CYPHER, style_ids=[s["id"] for s in matched]):
            item = _record(ItemRecord, ItemNode, r)
            minor = item.node.category_minor.value if item.node.category_minor else None
            major = item.category_major.value if item.category_major else None
            if cats.include and not any(_ref_matches(x, major, minor) for x in cats.include):
                continue
            if any(_ref_matches(x, major, minor) for x in cats.exclude):
                continue
            ok, res = item_passes(item, query.requested.attributes)
            if not ok:
                dropped_attr += 1
                continue
            by_style.setdefault(r["sid"], []).append((item, res))
        if dropped_attr:
            limitations.append(f"요청 속성과 명시적으로 달라 제외한 아이템 {dropped_attr}개.")

        # 3. Product 후보 → 점수 → 순위
        trends_raw: list[tuple[dict[str, Any], list[tuple[ItemRecord, MatchResult, list[ScoredProduct]]]]] = []
        excluded = {"unknown": 0, "mismatch": 0}
        weak_items: list[str] = []
        uses_snap = False
        for s in matched:
            rows = []
            for item, res in by_style.get(s["id"], []):
                core, supporting = s.get("focus_core", []), s.get("focus_supporting", [])
                has_attrs = any(attribute_values(item, a) for a in SCORED_ATTRIBUTES)
                fast = (not has_attrs) and not query.requested.attributes
                ids = self.candidate_ids(item, limit_when_no_attributes=self.top_k if fast else None)
                products = self._fetch_products(ids)
                if query.requested.attributes:
                    kept = []
                    for p in products:
                        ok, why = product_passes(p, query.requested.attributes)
                        if ok:
                            kept.append(p)
                        else:
                            excluded[why] += 1
                    products = kept
                ranked = rank_products(item, products, self.weights, core, supporting, top_k=self.top_k)
                if ranked and ranked[0].compared == 0:
                    weak_items.append(item.node.name)
                uses_snap |= any(r.product.node.source == "musinsa_snap" for r in ranked)
                rows.append((item, res, ranked))
            if rows:
                trends_raw.append((s, rows))
        if excluded["unknown"] or excluded["mismatch"]:
            limitations.append(f"요청 속성을 충족한다는 근거가 없어 제외한 상품: 값 없음 {excluded['unknown']}건, 다른 값 {excluded['mismatch']}건.")
        if weak_items:
            limitations.append("속성이 없는 아이템은 하분류가 같다는 것만으로 후보를 골랐습니다 (순위 근거가 약함): " + ", ".join(dict.fromkeys(weak_items)))
        if uses_snap:
            limitations.append("musinsa_snap 상품은 착장 사진 속 아이템이라 구매 가능한 상품이 아니며 색 정보가 없습니다 (kind=snap_garment).")
        if not trends_raw:
            limitations.append("조건을 통과한 트렌드·아이템이 없습니다.")
            return TrendAnalysisResult(request_id=query.request_id, status="no_results", applied_filters=applied, limitations=limitations)

        # 4. 근거
        node_ids = [s["id"] for s, _ in trends_raw] + [it.node.id for _, rows in trends_raw for it, _, _ in rows]
        evidence: dict[tuple[str, int, str], EvidenceRecord] = {}
        ev_by_node: dict[str, list[dict[str, Any]]] = {}
        for r in self._q(
            "MATCH (n)-[e:EVIDENCED_BY]->(a:Article) WHERE n.id IN $ids "
            "RETURN n.id AS nid, e.block_idx AS b, e.quote AS q, e.about AS about, e.idx AS idx, "
            "a.id AS aid, a.title AS title, a.url AS url, a.published_at AS pub ORDER BY nid, about, idx, b", ids=node_ids):
            k = (r["aid"], r["b"], r["q"])
            if k not in evidence:
                evidence[k] = EvidenceRecord(evidence_id=f"ev{len(evidence) + 1:03d}", article_id=r["aid"], title=r["title"],
                                             url=r["url"], published_at=r["pub"], block_idx=r["b"], quote=r["q"])
            ev_by_node.setdefault(r["nid"], []).append({"id": evidence[k].evidence_id, "about": r["about"], "idx": r["idx"]})

        trends: list[Trend] = []
        for s, rows in trends_raw:
            evs = ev_by_node.get(s["id"], [])
            descs = s.get("description", [])
            style = TrendStyle(
                id=s["id"], name=s["name"], name_origin=s["name_origin"], seasons=s.get("seasons", []), definition=s["definition"],
                description=[DescriptionOut(text=t, evidence_ids=[e["id"] for e in evs if e["about"] == "description" and e["idx"] == n])
                             for n, t in enumerate(descs)],
                focus_core=s.get("focus_core", []), focus_supporting=s.get("focus_supporting", []),
                focus_evidence_ids=[e["id"] for e in evs if e["about"] == "focus"])
            items = []
            for item, res, ranked in rows:
                n = item.node
                items.append(TrendItem(
                    id=n.id, name=n.name, styling_raw=n.styling_raw, styling_accessory=n.styling_accessory, gender=n.gender,
                    request_match=res, image_urls=n.image_urls, evidence_ids=[e["id"] for e in ev_by_node.get(n.id, [])],
                    similar_products=[_product_match(sp) for sp in ranked], **attributes_out(item)))
            trends.append(Trend(style=style, items=items))
        return TrendAnalysisResult(request_id=query.request_id, status="ok", applied_filters=applied, trends=trends,
                                   evidence=list(evidence.values()), limitations=limitations)


def _product_match(sp: ScoredProduct) -> ProductMatch:
    n = sp.product.node
    return ProductMatch(product_id=n.id, name=n.name, image_url=n.image_url, source=n.source, kind=n.kind,
                        sub_colors=n.sub_colors, match=sp.match, score=round(sp.score, 4), score_breakdown=sp.breakdown,
                        **attributes_out(sp.product))
