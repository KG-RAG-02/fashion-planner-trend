"""질의 → Style·Item 검색 → Product 후보 탐색·순위 → `TrendAnalysisResult`. 문서: docs/spec.md Step 8~10

- Style: 요청 시즌이 `Style.seasons`에 포함된 것 (시즌 미확인 Style은 보류하고 limitations에 알린다).
- Item: 카테고리 필터(상분류·하분류 include/exclude) → 요청 속성 필터 (명시적으로 다르면 제거, 미확인은 통과).
- Product 후보: **상분류가 같고** 하분류·핏·공유 속성 노드(색·소재·패턴·디테일) 중 하나가 같은 Product를 그래프에서 모으고,
  점수는 `retrieval/scoring.py`가 계산한다 (DB 없이도 같은 점수를 낼 수 있다).

v1.1: 후보 탐색은 **속성 노드에서 출발**한다(Item 노드가 없는 가상 Item도 같은 경로 — Resolver Tier2·4).
사용 가능 값(approx 제외)마다 `(:Color {id})<-[:HAS_COLOR]-(p)`를 찾으므로 v1.0의 Item 출발 쿼리와 같은 집합이다.
근거 id는 안정 규칙 `evidence_id()`, 이름 질의의 실패 사유(이름 없음·시즌 불일치·시즌 미확인)는 서로 다른 문장으로 남긴다.
조립 메서드(`styles`·`style_items`·`rank_item`·`evidence_for`·`trend_style`·`trend_item`)는 Resolver가 함께 쓴다.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from neo4j import Driver

from trend_agent.retrieval.scoring import (
    ScoredProduct,
    ScoringOptions,
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
    evidence_id,
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
STYLES_CYPHER = "MATCH (s:Style) RETURN s ORDER BY s.id"
EVIDENCE_CYPHER = (
    "MATCH (n)-[e:EVIDENCED_BY]->(a:Article) WHERE n.id IN $ids "
    "RETURN n.id AS nid, e.block_idx AS b, e.quote AS q, e.about AS about, e.idx AS idx, "
    "a.id AS aid, a.title AS title, a.url AS url, a.published_at AS pub ORDER BY nid, about, idx, b")
# 속성 노드 출발 후보 (approx 관계 제외). Detail 노드 id는 "<type>:<name>" = attribute_values("detail")의 값.
_ATTR_CANDIDATES = {
    attr: f"UNWIND $vals AS v MATCH (:{label} {{id: v}})<-[r:{rel}]-(p:Product)-[:IN_CATEGORY]->(:Category {{id: $major}}) "
          "WHERE NOT coalesce(r.approx, false) RETURN DISTINCT p.id AS id"
    for attr, label, rel in (("color", "Color", "HAS_COLOR"), ("material", "Material", "HAS_MATERIAL"),
                             ("pattern", "Pattern", "HAS_PATTERN"), ("detail", "Detail", "HAS_DETAIL"))
}
# ProductNode.has_image와 같은 정의 (status가 있으면 == 'ok', 없으면 참조 존재)
HAS_IMAGE_CYPHER = ("CASE WHEN p.image_status IS NOT NULL THEN p.image_status = 'ok' "
                    "ELSE (p.image_url IS NOT NULL OR p.image_ref IS NOT NULL) END")


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


def _v(x: Any) -> Any:
    return x.value if hasattr(x, "value") else x


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
    def __init__(self, driver: Driver, weights: Optional[WeightsConfig] = None, top_k: int = 5,
                 options: Optional[ScoringOptions] = None):
        self.driver = driver
        self.weights = weights or load_weights()
        self.top_k = top_k
        self.options = options or ScoringOptions.from_weights(self.weights)  # 기본 = flags (현재 yaml은 v1.0 동작)
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

    def candidate_ids(self, item: ItemRecord, limit_when_no_attributes: Optional[int] = None,
                      image_first: bool = False) -> list[str]:
        """Item과 상분류가 같고, 하분류·핏·공유 속성 노드 중 하나가 같은 Product의 id.

        속성 노드에서 출발하므로 그래프에 없는 가상 Item(ItemRecord만 있음)에도 쓸 수 있다.
        `limit_when_no_attributes`(fast path)는 하분류 일치 상품을 [이미지 우선(플래그)] → id 순으로 k개만 가져온다.
        """
        major = _v(item.category_major)
        ids: set[str] = set()
        minor = item.node.category_minor
        if minor is not None:
            order = ""
            if limit_when_no_attributes:
                img = f"{HAS_IMAGE_CYPHER} DESC, " if image_first else ""
                order = f" ORDER BY {img}p.id LIMIT {int(limit_when_no_attributes)}"
            ids |= {r["id"] for r in self._q(
                "MATCH (p:Product {category_minor: $minor})-[:IN_CATEGORY]->(:Category {id: $major}) RETURN p.id AS id" + order,
                minor=_v(minor), major=major)}
        if limit_when_no_attributes:
            return sorted(ids)
        fit = item.usable_scalar("fit")
        if fit is not None:
            ids |= {r["id"] for r in self._q(
                "MATCH (p:Product {fit: $fit})-[:IN_CATEGORY]->(:Category {id: $major}) WHERE NOT 'fit' IN p.approx RETURN p.id AS id",
                fit=_v(fit), major=major)}
        for attr, cypher in _ATTR_CANDIDATES.items():
            vals = sorted(attribute_values(item, attr))
            if vals:
                ids |= {r["id"] for r in self._q(cypher, vals=vals, major=major)}
        return sorted(ids)

    def fast_path(self, item: ItemRecord, has_requested: bool = False, options: Optional[ScoringOptions] = None) -> bool:
        """속성이 없는 Item은 하분류 일치 보너스만 받으므로 상위 k개만 가져와도 전수 결과와 같다.
        성별 필터·snap 제한이 켜져 있으면 k개 뒤의 상품이 올라올 수 있어 쓰지 않는다."""
        o = options or self.options
        if has_requested or o.item_gender_filter or o.snap_id_limit:
            return False
        return not any(attribute_values(item, a) for a in SCORED_ATTRIBUTES)

    def candidates(self, item: ItemRecord, top_k: Optional[int] = None, has_requested: bool = False,
                   options: Optional[ScoringOptions] = None) -> list[str]:
        """search()·Resolver·verify_graph_search가 같은 규칙으로 쓰는 후보 id."""
        o = options or self.options
        k = top_k or self.top_k
        fast = self.fast_path(item, has_requested, o)
        return self.candidate_ids(item, limit_when_no_attributes=k if fast else None, image_first=o.image_first_tiebreak)

    def rank_item(self, item: ItemRecord, focus_core: Iterable[str] = (), focus_supporting: Iterable[str] = (), *,
                  top_k: Optional[int] = None, requested: Iterable[Any] = (), options: Optional[ScoringOptions] = None,
                  excluded: Optional[dict[str, int]] = None, stats: Optional[dict[str, int]] = None) -> list[ScoredProduct]:
        """Item 하나의 후보 → (요청 속성 필터) → 점수·순위."""
        o = options or self.options
        k = top_k or self.top_k
        requested = list(requested)
        products = self._fetch_products(self.candidates(item, k, bool(requested), o))
        if requested:
            kept = []
            for p in products:
                ok, why = product_passes(p, requested)
                if ok:
                    kept.append(p)
                elif excluded is not None:
                    excluded[why] = excluded.get(why, 0) + 1
            products = kept
        return rank_products(item, products, self.weights, focus_core, focus_supporting, top_k=k, options=o, stats=stats)

    # ---------- 조립 (search()와 Resolver 공용) ----------
    def styles(self) -> list[dict[str, Any]]:
        return [dict(r["s"]) for r in self._q(STYLES_CYPHER)]

    def style_items(self, style_ids: list[str]) -> dict[str, list[ItemRecord]]:
        out: dict[str, list[ItemRecord]] = {}
        for r in self._q(ITEMS_CYPHER, style_ids=style_ids):
            out.setdefault(r["sid"], []).append(_record(ItemRecord, ItemNode, r))
        return out

    def evidence_for(self, node_ids: list[str]) -> tuple[dict[str, EvidenceRecord], dict[str, list[dict[str, Any]]]]:
        """노드의 EVIDENCED_BY → (evidence_id → 근거, 노드 id → [{id, about, idx}]). id는 안정 규칙 evidence_id()."""
        evidence: dict[str, EvidenceRecord] = {}
        ev_by_node: dict[str, list[dict[str, Any]]] = {}
        for r in self._q(EVIDENCE_CYPHER, ids=node_ids):
            eid = evidence_id(r["aid"], r["b"], r["q"])
            if eid not in evidence:
                evidence[eid] = EvidenceRecord(evidence_id=eid, article_id=r["aid"], title=r["title"], url=r["url"],
                                               published_at=r["pub"], block_idx=r["b"], quote=r["q"])
            ev_by_node.setdefault(r["nid"], []).append({"id": eid, "about": r["about"], "idx": r["idx"]})
        return evidence, ev_by_node

    @staticmethod
    def trend_style(s: dict[str, Any], evs: list[dict[str, Any]], season_status: Optional[str] = None) -> TrendStyle:
        descs = s.get("description", [])
        return TrendStyle(
            id=s["id"], name=s["name"], name_origin=s["name_origin"], seasons=s.get("seasons", []), definition=s["definition"],
            description=[DescriptionOut(text=t, evidence_ids=[e["id"] for e in evs if e["about"] == "description" and e["idx"] == n])
                         for n, t in enumerate(descs)],
            focus_core=s.get("focus_core", []), focus_supporting=s.get("focus_supporting", []),
            focus_evidence_ids=[e["id"] for e in evs if e["about"] == "focus"],
            aliases=s.get("aliases", []), article_id=s.get("article_id"), season_status=season_status)

    @staticmethod
    def trend_item(item: ItemRecord, ranked: list[ScoredProduct], evidence_ids: list[str],
                   request_match: Optional[MatchResult] = None, **v11: Any) -> TrendItem:
        n = item.node
        return TrendItem(
            id=n.id, name=n.name, styling_raw=n.styling_raw, styling_accessory=n.styling_accessory, gender=n.gender,
            request_match=request_match or MatchResult(), image_urls=n.image_urls, evidence_ids=evidence_ids,
            similar_products=[product_match(sp) for sp in ranked], **attributes_out(item), **v11)

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

        # 1. Style (시즌, 이름) — 시즌만 주는 질의는 strict(v1.0). 실패 사유는 이름 없음·시즌 불일치·시즌 미확인을 구분한다.
        styles = self.styles()
        terms = [_norm(t.raw) for t in query.trend_terms.style_names]
        pool = styles
        if terms:
            pool = [s for s in styles if any(t in _norm(s["name"]) or _norm(s["name"]) in t for t in terms)]
            if not pool:
                names = ", ".join(t.raw for t in query.trend_terms.style_names)
                limitations.append(f"요청한 트렌드명({names})과 일치하는 트렌드가 그래프에 없습니다.")
                return TrendAnalysisResult(request_id=query.request_id, status="no_results", applied_filters=applied, limitations=limitations)
        unknown_season = [s for s in pool if not s.get("seasons")]
        mismatch_season = [s for s in pool if s.get("seasons") and not style_matches_season(s["seasons"], season)]
        matched = [s for s in pool if style_matches_season(s.get("seasons", []), season)]
        if unknown_season:
            limitations.append(f"시즌이 확인되지 않은 트렌드 {len(unknown_season)}개는 시즌 조건 검색에서 보류했습니다."
                               + (" (" + ", ".join(s["name"] for s in unknown_season) + ")" if terms else ""))
        if terms and mismatch_season:
            limitations.append(f"이름은 일치하지만 {key} 시즌이 아닌 트렌드 {len(mismatch_season)}개를 제외했습니다: "
                               + ", ".join(f"{s['name']}({'·'.join(s['seasons'])})" for s in mismatch_season))
        if not matched:
            if not terms:
                limitations.append(f"{key}에 유행한 트렌드가 그래프에 없습니다.")
            return TrendAnalysisResult(request_id=query.request_id, status="no_results", applied_filters=applied, limitations=limitations)

        # 2. Item (카테고리 → 요청 속성)
        by_style: dict[str, list[tuple[ItemRecord, MatchResult]]] = {}
        dropped_attr = 0
        for sid, items in self.style_items([s["id"] for s in matched]).items():
            for item in items:
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
                by_style.setdefault(sid, []).append((item, res))
        if dropped_attr:
            limitations.append(f"요청 속성과 명시적으로 달라 제외한 아이템 {dropped_attr}개.")

        # 3. Product 후보 → 점수 → 순위
        trends_raw: list[tuple[dict[str, Any], list[tuple[ItemRecord, MatchResult, list[ScoredProduct]]]]] = []
        excluded = {"unknown": 0, "mismatch": 0}
        weak_items: list[str] = []
        uses_snap = False
        stats: dict[str, int] = {}
        for s in matched:
            rows = []
            for item, res in by_style.get(s["id"], []):
                core, supporting = s.get("focus_core", []), s.get("focus_supporting", [])
                ranked = self.rank_item(item, core, supporting, requested=query.requested.attributes,
                                        excluded=excluded, stats=stats)
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
        limitations += scoring_limitations(stats)
        if not trends_raw:
            limitations.append("조건을 통과한 트렌드·아이템이 없습니다.")
            return TrendAnalysisResult(request_id=query.request_id, status="no_results", applied_filters=applied, limitations=limitations)

        # 4. 근거 (안정 evidence_id)
        node_ids = [s["id"] for s, _ in trends_raw] + [it.node.id for _, rows in trends_raw for it, _, _ in rows]
        evidence, ev_by_node = self.evidence_for(node_ids)

        trends: list[Trend] = []
        for s, rows in trends_raw:
            style = self.trend_style(s, ev_by_node.get(s["id"], []), season_status="match")
            items = [self.trend_item(item, ranked, [e["id"] for e in ev_by_node.get(item.node.id, [])], res)
                     for item, res, ranked in rows]
            trends.append(Trend(style=style, items=items))
        return TrendAnalysisResult(request_id=query.request_id, status="ok", applied_filters=applied, trends=trends,
                                   evidence=list(evidence.values()), limitations=limitations)


def scoring_limitations(stats: dict[str, int]) -> list[str]:
    """점수 플래그로 걸러낸 건수 (플래그가 꺼져 있으면 빈 목록)."""
    out = []
    if stats.get("gender_excluded"):
        out.append(f"성별 필터(G0-③)로 Item과 반대 성별 상품 {stats['gender_excluded']}건을 제외했습니다.")
    if stats.get("snap_limited"):
        out.append(f"같은 스냅 사진(snap_id)의 상품 수 제한으로 {stats['snap_limited']}건을 뺐습니다.")
    return out


def product_match(sp: ScoredProduct) -> ProductMatch:
    n = sp.product.node
    return ProductMatch(product_id=n.id, name=n.name, image_url=n.image_url, source=n.source, kind=n.kind,
                        sub_colors=n.sub_colors, match=sp.match, score=round(sp.score, 4), score_breakdown=sp.breakdown,
                        gender=n.gender, snap_id=n.snap_id, image_ref=n.image_ref, image_scope=n.image_scope,
                        image_status=n.image_status, parent_id=n.parent_id, multi_wearer_suspected=n.multi_wearer_suspected,
                        has_image=n.has_image, **attributes_out(sp.product))


_product_match = product_match  # v1.0 이름 (호환)
