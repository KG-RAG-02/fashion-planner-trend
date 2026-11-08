"""Item과 Product의 일치도. 문서: docs/spec.md Step 8.3, Step 9, docs/schemas/trend_analysis_result.md

점수 식 (spec Step 9):
  base_score = Σ(속성의 적용 가중치 × 일치 비율) ÷ Σ(비교 가능한 속성의 적용 가중치)   (0~1)
  score      = base_score + 하분류 일치 보너스 (α × 트렌드 배수)
- 상분류(category_major)가 같은 Product만 후보다 (점수가 아니라 조건).
- 비교 가능한 속성 = Item에 값이 있고 Product에도 값이 있는 속성. Item에 값이 없으면 분자·분모에서 빠지고(가중치 0),
  Product에 값이 없으면 `unknown`으로 표시하고 빠진다. 불일치로 감점하지 않는다.
- 근사(approx) 값은 쓰지 않는다 (미확인 취급). Length·Shape·서브컬러는 점수에 쓰지 않는다.

v1.1 (계약 G1, 모두 기본값 = v1.0 동작 — `ScoringOptions()`):
- 값 가중 재현율: ratio = Σ_{공통} w(v) ÷ Σ_{Item 값} w(v). 값 가중치가 없으면 |공통|/|Item| 그대로 계산한다.
- core 미확인 감쇠 γ: base = Σ w·r ÷ (Σ w + γ·Σ_{core ∧ Item 값 있음 ∧ Product 미확인} w).
- 이미지 동점 정렬(id 바로 앞에 -has_image), snap_id 개수 제한, Item 성별 필터(남/여와 반대 성별 제외).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from trend_agent.schemas.graph_schema import SCORED_ATTRIBUTES, ItemRecord, ProductRecord, _Record
from trend_agent.schemas.trend_analysis_result import AttributeScore, MatchResult, ScoreBreakdown
from trend_agent.schemas.trend_query import RequestedAttribute

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
    """점수·정렬 플래그 묶음. 기본값 = v1.0 동작 (weights.yaml `flags` 기본값과 같다)."""

    value_weights: Optional[ValueWeights] = None
    core_unknown_decay: float = 0.0
    image_first_tiebreak: bool = False
    snap_id_limit: int = 0
    item_gender_filter: bool = False

    @classmethod
    def from_weights(cls, weights: WeightsConfig, *, tier4: bool = False,
                     value_weights: Optional[ValueWeights] = None) -> "ScoringOptions":
        """weights.yaml `flags`로 만든다. Tier4 모드에서는 γ = `tier4.core_unknown_decay`."""
        f = weights.flags
        gamma = weights.tier4.core_unknown_decay if tier4 else f.core_unknown_decay
        return cls(value_weights=value_weights, core_unknown_decay=gamma, image_first_tiebreak=f.image_first_tiebreak,
                   snap_id_limit=f.snap_id_limit, item_gender_filter=f.item_gender_filter)


DEFAULT_OPTIONS = ScoringOptions()


@dataclass
class ScoredProduct:
    product: ProductRecord
    score: float
    breakdown: ScoreBreakdown
    match: MatchResult
    compared: int = 0  # 비교된 속성 수 (동점 정렬에 쓴다)
    image_first: bool = False  # flags.image_first_tiebreak (켜면 id 바로 앞에서 이미지 있는 상품 우선)

    @property
    def has_image(self) -> bool:
        return self.product.node.has_image

    @property
    def sort_key(self) -> tuple:
        # 같은 입력은 같은 순위: 점수 → 비교된 속성이 많은 쪽 → 일치 값이 많은 쪽 → [이미지 있는 쪽] → id
        img = -int(self.has_image) if self.image_first else 0
        return (-round(self.score, 9), -self.compared, -len(self.match.matched), img, self.product.node.id)


def _value_ratio(iv: set[str], common: set[str], vw: Optional[Mapping[str, float]]) -> float:
    if not vw:
        return len(common) / len(iv)  # v1.0 식 그대로 (비트 단위 동일)
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
    unknown_core = 0.0
    for attr in SCORED_ATTRIBUTES:
        iv = attribute_values(item, attr)
        if not iv:
            continue  # Item에 값이 없다 → 비교하지 않는다 (가중치 0)
        pv = attribute_values(product, attr)
        if not pv:
            unknown.append(attr)  # Product 미확인
            if options.core_unknown_decay and attr in core:
                unknown_core += options.core_unknown_decay * weights.applied_weight(attr, core, supporting)
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
    if unknown_core:
        base = num / (den + unknown_core)
    else:
        base = num / den if den else 0.0
    bonus: dict[str, float] = {}
    im, pm = category_minor_of(item), category_minor_of(product)
    if im is not None and im == pm:
        bonus["category_minor"] = weights.applied_alpha(core, supporting)
    return ScoredProduct(
        product=product,
        score=base + sum(bonus.values()),
        breakdown=ScoreBreakdown(attributes=attrs, base_score=base, bonus=bonus, unknown_core_weight=unknown_core,
                                 value_weighted=bool(vws)),
        match=MatchResult(matched=matched, mismatched=mismatched, unknown=unknown),
        compared=len(attrs),
        image_first=options.image_first_tiebreak,
    )


def gender_compatible(item: ItemRecord, product: ProductRecord) -> bool:
    """G0-③ 성별 필터: Item이 남/여면 반대 성별 상품만 제외한다. 공용·미상(상품이든 Item이든)은 통과."""
    ig, pg = _s(item.node.gender), _s(getattr(product.node, "gender", None))
    if ig not in ("남", "여") or pg not in ("남", "여"):
        return True
    return ig == pg


def limit_per_snap(scored: list[ScoredProduct], limit: int) -> list[ScoredProduct]:
    """정렬된 목록에서 같은 snap_id(같은 사진)는 앞에서부터 `limit`개만 남긴다. snap_id 없는 상품은 그대로."""
    if limit <= 0:
        return scored
    seen: dict[int, int] = {}
    out = []
    for s in scored:
        sid = s.product.node.snap_id
        if sid is not None:
            seen[sid] = seen.get(sid, 0) + 1
            if seen[sid] > limit:
                continue
        out.append(s)
    return out


def rank_products(
    item: ItemRecord,
    products: Iterable[ProductRecord],
    weights: WeightsConfig,
    focus_core: Iterable[str] = (),
    focus_supporting: Iterable[str] = (),
    top_k: Optional[int] = None,
    min_score: float = 0.0,
    options: ScoringOptions = DEFAULT_OPTIONS,
    stats: Optional[dict[str, int]] = None,
) -> list[ScoredProduct]:
    """점수가 `min_score`를 넘는 Product를 순서대로. 아무것도 맞지 않은 후보(점수 0)는 내지 않는다.

    Neo4j 없이 호출할 수 있는 순수 함수다(T4 평가용). `stats`를 주면 `gender_excluded`·`snap_limited` 건수를 더한다.
    """
    products = list(products)
    if options.item_gender_filter:
        kept = [p for p in products if gender_compatible(item, p)]
        if stats is not None:
            stats["gender_excluded"] = stats.get("gender_excluded", 0) + len(products) - len(kept)
        products = kept
    scored = [s for p in products if (s := score_product(item, p, weights, focus_core, focus_supporting, options)) is not None]
    scored = [s for s in scored if s.score > min_score]
    scored.sort(key=lambda s: s.sort_key)
    if options.snap_id_limit:
        n = len(scored)
        scored = limit_per_snap(scored, options.snap_id_limit)
        if stats is not None:
            stats["snap_limited"] = stats.get("snap_limited", 0) + n - len(scored)
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
