"""Item과 Product의 일치도. 문서: docs/spec.md Step 8.3, Step 9, docs/schemas/trend_analysis_result.md

점수 식 (spec Step 9):
  base_score = Σ(속성의 적용 가중치 × 일치 비율) ÷ Σ(비교 가능한 속성의 적용 가중치)   (0~1)
  score      = base_score + 하분류 일치 보너스 (α × 트렌드 배수)
- 상분류(category_major)가 같은 Product만 후보다 (점수가 아니라 조건).
- 비교 가능한 속성 = Item에 값이 있고 Product에도 값이 있는 속성. Item에 값이 없으면 분자·분모에서 빠지고(가중치 0),
  Product에 값이 없으면 `unknown`으로 표시하고 빠진다. 불일치로 감점하지 않는다.
- 근사(approx) 값은 쓰지 않는다 (미확인 취급). Length·Shape·서브컬러는 점수에 쓰지 않는다.

선택: 값 가중 재현율 ratio = Σ_{공통} w(v) ÷ Σ_{Item 값} w(v) (`ScoringOptions.value_weights`).
값 가중치가 없으면 |공통|/|Item| 그대로 계산한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from fashion_planner_trend.schemas.graph_schema import SCORED_ATTRIBUTES, ItemRecord, ProductRecord, _Record
from fashion_planner_trend.schemas.trend_analysis_result import AttributeScore, MatchResult, ScoreBreakdown
from fashion_planner_trend.schemas.trend_query import RequestedAttribute

from .weights import WeightsConfig


def _s(v: Any) -> Any:
    return v.value if isinstance(v, Enum) else v


def attribute_values(rec: _Record, attribute: str) -> set[str]:
    """검색·점수에 쓸 수 있는(근사가 아닌) 값. detail은 "<type>:<name>"."""
    if attribute == "fit":
        v = rec.usable_scalar("fit")
        return {_s(v)} if v is not None else set()
    if attribute == "detail":
        return {f"{_s(e.type)}:{e.value}" for e in rec.usable("detail")}
    return {e.value for e in rec.usable(attribute)}


def _label(attr: str, value: str) -> str:
    """표시용. detail 값은 이미 "<type>:<name>"이라 접두사를 또 붙이지 않는다."""
    return value if attr == "detail" else f"{attr}:{value}"


def category_minor_of(rec: _Record) -> Optional[str]:
    return _s(rec.node.category_minor) if rec.node.category_minor is not None else None


ValueWeights = Mapping[str, Mapping[str, float]]  # {속성: {값: 가중치}} — 값이 없으면 1.0


@dataclass(frozen=True)
class ScoringOptions:
    """점수 선택 사항. 기본값은 값 가중치 없음(|공통|/|Item|)."""

    value_weights: Optional[ValueWeights] = None


DEFAULT_OPTIONS = ScoringOptions()


@dataclass
class ScoredProduct:
    product: ProductRecord
    score: float
    breakdown: ScoreBreakdown
    match: MatchResult
    compared: int = 0  # 비교된 속성 수 (동점 정렬에 쓴다)

    @property
    def sort_key(self) -> tuple:
        # 같은 입력은 같은 순위: 점수 → 비교된 속성이 많은 쪽 → 일치 값이 많은 쪽 → id
        return (-round(self.score, 9), -self.compared, -len(self.match.matched), self.product.node.id)


def _value_ratio(iv: set[str], common: set[str], vw: Optional[Mapping[str, float]]) -> float:
    if not vw:
        return len(common) / len(iv)
    total = sum(vw.get(v, 1.0) for v in iv)
    return sum(vw.get(v, 1.0) for v in common) / total if total else 0.0


def score_product(
    item: ItemRecord,
    product: ProductRecord,
    weights: WeightsConfig,
    focus_core: Iterable[str] = (),
    focus_supporting: Iterable[str] = (),
    options: ScoringOptions = DEFAULT_OPTIONS,
) -> Optional[ScoredProduct]:
    """후보가 아니면(상분류가 다르면) None."""
    if item.category_major is None or item.category_major != product.category_major:
        return None
    core, supporting = set(focus_core), set(focus_supporting)
    vws = options.value_weights or {}
    attrs: dict[str, AttributeScore] = {}
    matched: list[str] = []
    mismatched: list[str] = []
    unknown: list[str] = []
    num = den = 0.0
    for attr in SCORED_ATTRIBUTES:
        iv = attribute_values(item, attr)
        if not iv:
            continue  # Item에 값이 없다 → 비교하지 않는다 (가중치 0)
        pv = attribute_values(product, attr)
        if not pv:
            unknown.append(attr)  # Product 미확인
            continue
        common = iv & pv
        ratio = _value_ratio(iv, common, vws.get(attr))
        w = weights.applied_weight(attr, core, supporting)
        attrs[attr] = AttributeScore(
            weight=w, base_weight=weights.base_weights[attr],
            level="core" if attr in core else "supporting" if attr in supporting else None, ratio=ratio)
        num += w * ratio
        den += w
        matched += [_label(attr, v) for v in sorted(common)]
        mismatched += [_label(attr, v) for v in sorted(iv - pv)]
    base = num / den if den else 0.0
    bonus: dict[str, float] = {}
    im, pm = category_minor_of(item), category_minor_of(product)
    if im is not None and im == pm:
        bonus["category_minor"] = weights.applied_alpha(core, supporting)
    return ScoredProduct(
        product=product,
        score=base + sum(bonus.values()),
        breakdown=ScoreBreakdown(attributes=attrs, base_score=base, bonus=bonus, value_weighted=bool(vws)),
        match=MatchResult(matched=matched, mismatched=mismatched, unknown=unknown),
        compared=len(attrs),
    )


def rank_products(
    item: ItemRecord,
    products: Iterable[ProductRecord],
    weights: WeightsConfig,
    focus_core: Iterable[str] = (),
    focus_supporting: Iterable[str] = (),
    top_k: Optional[int] = None,
    min_score: float = 0.0,
    options: ScoringOptions = DEFAULT_OPTIONS,
) -> list[ScoredProduct]:
    """점수가 `min_score`를 넘는 Product를 순서대로. 아무것도 맞지 않은 후보(점수 0)는 내지 않는다.

    Neo4j 없이 호출할 수 있는 순수 함수다.
    """
    scored = [s for p in products if (s := score_product(item, p, weights, focus_core, focus_supporting, options)) is not None]
    scored = [s for s in scored if s.score > min_score]
    scored.sort(key=lambda s: s.sort_key)
    return scored[:top_k] if top_k else scored


# ---------- 요청 속성 필터 (spec Step 8.3) ----------
def _requested_by_type(requested: Iterable[RequestedAttribute]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in requested:
        if r.value is not None:  # 표준 값으로 못 바꾼 요청은 필터에 쓸 수 없다
            out.setdefault(r.type, set()).add(r.value)
    return out


def check_requested(rec: _Record, requested: Iterable[RequestedAttribute]) -> MatchResult:
    """요청 속성과 비교. 같은 속성의 여러 값은 OR. 값이 없으면 unknown, 있는데 겹치지 않으면 mismatched."""
    res = MatchResult()
    for attr, wanted in _requested_by_type(requested).items():
        have = attribute_values(rec, attr)
        if attr == "detail":  # 요청의 detail은 이름만 있어 type을 모른다
            have = {h.split(":", 1)[1] for h in have}
        if not have:
            res.unknown.append(attr)
        elif have & wanted:
            res.matched += [f"{attr}:{v}" for v in sorted(have & wanted)]
        else:
            res.mismatched += [f"{attr}:{v}" for v in sorted(wanted)]
    return res


def item_passes(rec: ItemRecord, requested: Iterable[RequestedAttribute]) -> tuple[bool, MatchResult]:
    """Item: 요청과 명시적으로 다르면 제거, 값이 없으면 통과(unknown)."""
    res = check_requested(rec, requested)
    return (not res.mismatched), res


def product_passes(rec: ProductRecord, requested: Iterable[RequestedAttribute]) -> tuple[bool, str]:
    """Product: 요청을 충족한다는 근거가 있어야 통과. 반환 (통과 여부, 'ok' | 'mismatch' | 'unknown')."""
    res = check_requested(rec, requested)
    if res.mismatched:
        return False, "mismatch"
    if res.unknown:
        return False, "unknown"
    return True, "ok"
