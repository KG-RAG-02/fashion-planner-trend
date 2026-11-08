"""트렌드 단위 Top-K — "트렌드에 가장 가까운 의상 종합". 문서: trend_analysis_result.md §3.2 (계약 v1.1)

Item마다 상위 `pool_per_item`개 후보(점수 순위)를 받아:
1. 상품별 `trend_score = max(score × item_focus)` (출처 Item = 최고점 Item, 나머지 Item은 also_from_items)
2. 중복 제거 키(앞에서부터) `snap_id`(같은 사진) → `parent_id`(같은 세트) → `dup_of`(원 id로 묶음) → `product_id`
   — 같은 키에서는 trend_score가 가장 높은 한 줄만 남긴다.
3. 정렬 (-trend_score, -비교 속성 수, -일치 값 수, [-has_image], product_id) → 상위 K개에 rank.
Neo4j 없이 호출하는 순수 함수다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from trend_agent.retrieval.scoring import ScoredProduct
from trend_agent.schemas.trend_analysis_result import ProductMatch, TrendPick


@dataclass
class ItemRanking:
    item_id: str
    item_name: str
    item_focus: float
    ranked: list[ScoredProduct]


def dedup_key(sp: ScoredProduct) -> tuple:
    n = sp.product.node
    if n.snap_id is not None:
        return ("snap", n.snap_id)
    if n.parent_id:
        return ("parent", n.parent_id)
    return ("id", n.dup_of or n.id)


def _order(ts: float, sp: ScoredProduct, image_first: bool) -> tuple:
    img = -int(sp.has_image) if image_first else 0
    return (-round(ts, 9), -sp.compared, -len(sp.match.matched), img, sp.product.node.id)


def trend_topk(rankings: Iterable[ItemRanking], k: int, image_first: bool = False,
               to_match=None) -> list[TrendPick]:
    """`to_match`: ScoredProduct → ProductMatch (기본 graph.search.product_match)."""
    if to_match is None:
        from trend_agent.graph.search import product_match as to_match  # 순환 import 방지
    rows: list[tuple[float, ScoredProduct, ItemRanking]] = [
        (sp.score * r.item_focus, sp, r) for r in rankings for sp in r.ranked]
    groups: dict[tuple, list[tuple[float, ScoredProduct, ItemRanking]]] = {}
    for row in rows:
        groups.setdefault(dedup_key(row[1]), []).append(row)
    picks: list[tuple[tuple, float, ScoredProduct, ItemRanking, list[str]]] = []
    for members in groups.values():
        members.sort(key=lambda m: (_order(m[0], m[1], image_first), m[2].item_id))
        ts, sp, r = members[0]
        others = list(dict.fromkeys(m[2].item_id for m in members[1:] if m[2].item_id != r.item_id))
        picks.append((_order(ts, sp, image_first), ts, sp, r, others))
    picks.sort(key=lambda p: p[0])
    out: list[TrendPick] = []
    for rank, (_, ts, sp, r, others) in enumerate(picks[:k], 1):
        pm: ProductMatch = to_match(sp)
        out.append(TrendPick(**pm.model_dump(), rank=rank, trend_score=round(ts, 4), item_id=r.item_id,
                             item_name=r.item_name, item_focus=r.item_focus, also_from_items=others))
    return out
