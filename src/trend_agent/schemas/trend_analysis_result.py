"""트렌드 분석 최종 출력. 문서: docs/schemas/trend_analysis_result.md (계약 v1.1)

v1.1: status `partial`, 용어 단위 해석 결과(`TermResolution`)와 트렌드 Top-K(`TrendPick`), 판정 방식
(`match_mode`·`qualification`), 이미지 필드, 재현용 설정(`AppliedSettings`), 안정 evidence_id 규칙.
추가 필드는 모두 기본값이라 v1.0 JSON은 그대로 읽힌다.
"""
from __future__ import annotations

import hashlib
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .graph_schema import ImageScope, ImageStatus
from .trend_query import CategoryFilter, SeasonRequest
from .vocabulary import (
    BodyLength,
    CategoryMajor,
    CategoryMinor,
    Color,
    DetailType,
    Fit,
    Gender,
    Season,
    Shape,
    SleeveLength,
)

SCHEMA_VERSION = "1.1"

# 판정 방식 (Resolver Tier) → 근거 자격. graph·semantic은 기사 인용이 있고, attribute는 어휘 규칙, llm_inferred는 에이전트 제안이다.
MatchMode = Literal["graph", "attribute", "semantic", "llm_inferred"]
Qualification = Literal["article_evidence", "vocabulary_rule", "agent_proposal"]
QUALIFICATION_BY_MODE: dict[str, str] = {
    "graph": "article_evidence",
    "semantic": "article_evidence",
    "attribute": "vocabulary_rule",
    "llm_inferred": "agent_proposal",
}


def evidence_id(article_id: str, block_idx: int, quote: str) -> str:
    """안정 근거 ID `trend:<article_id>:<block_idx>:<hash8>` (hash8 = sha256(quote UTF-8) 앞 8자).

    같은 (기사, 블록, 인용)은 실행·질의가 달라도 같은 id다. quote는 저장된 문자열 그대로 해시한다(정규화하지 않는다).
    """
    return f"trend:{article_id}:{block_idx}:{hashlib.sha256(quote.encode('utf-8')).hexdigest()[:8]}"


def virtual_style_id(tier: int, norm: str) -> str:
    """그래프에 없는 용어로 만든 가상 트렌드(Tier2·4)의 id = "virtual:<tier>:<norm>"."""
    return f"virtual:{tier}:{norm}"


def virtual_item_id(tier: int, norm: str, k: int) -> str:
    """가상 트렌드의 k번째 Item id = "virtual:<tier>:<norm>-i<k>" (k는 LLM·규칙 출력 순번, 0부터)."""
    return f"{virtual_style_id(tier, norm)}-i{k}"


class AppliedAttribute(BaseModel):
    type: Literal["color", "material", "pattern", "detail", "fit"]
    value: str


class AppliedFilters(BaseModel):
    season: SeasonRequest
    categories: CategoryFilter = Field(default_factory=CategoryFilter)
    gender: Optional[Gender] = None
    attributes: list[AppliedAttribute] = Field(default_factory=list)


class DescriptionOut(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class TrendStyle(BaseModel):
    id: str
    name: str
    name_origin: Literal["article", "analyst", "query"]  # v1.1 "query": 가상 트렌드(이름 = 사용자 용어)
    seasons: list[str] = Field(default_factory=list)  # 이 트렌드가 유행했다고 기사가 말한 시즌
    definition: str
    description: list[DescriptionOut] = Field(default_factory=list)
    focus_core: list[str] = Field(default_factory=list)
    focus_supporting: list[str] = Field(default_factory=list)
    focus_evidence_ids: list[str] = Field(default_factory=list)
    # v1.1
    aliases: list[str] = Field(default_factory=list)
    article_id: Optional[str] = None
    season_status: Optional[Literal["match", "unknown", "mismatch", "not_checked"]] = None
    virtual: bool = False  # Tier2·4 가상 트렌드 (id = virtual_style_id())


class MatchResult(BaseModel):
    matched: list[str] = Field(default_factory=list)
    mismatched: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)  # Item에 명시되지 않은 속성은 불일치가 아니다


class AttributeScore(BaseModel):
    weight: float  # 적용 가중치 = 기본 가중치 × 트렌드 배수
    base_weight: float
    level: Optional[Literal["core", "supporting"]] = None
    ratio: float  # 일치 비율 0~1 — v1.1: 값 가중 재현율 Σ_{공통} w(v) ÷ Σ_{Item} w(v) (값 가중치가 없으면 |공통|/|Item|)


class ScoreBreakdown(BaseModel):
    attributes: dict[str, AttributeScore] = Field(default_factory=dict)
    base_score: float = 0.0  # v1.1: Σ w·r ÷ (Σ w + unknown_core_weight) — γ=0이면 v1.0 식과 같다
    bonus: dict[str, float] = Field(default_factory=dict)  # {"category_minor": α}
    unknown_core_weight: float = 0.0  # v1.1: γ × (미확인 core 속성의 적용 가중치 합)
    value_weighted: bool = False      # v1.1: 값 가중치(primary/secondary)를 적용했는가


class DetailOut(BaseModel):
    type: DetailType
    name: str
    raw: Optional[str] = None
    motif: Optional[str] = None
    placement: Optional[str] = None


class _AttributesOut(BaseModel):
    category_major: Optional[CategoryMajor] = None
    category_minor: Optional[CategoryMinor] = None
    colors: list[Color] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    details: list[DetailOut] = Field(default_factory=list)
    fit: Optional[Fit] = None
    shape: Optional[Shape] = None  # 표시만, 검색에 쓰지 않는다
    length_body: Optional[BodyLength] = None
    length_sleeve: Optional[SleeveLength] = None
    length_body_raw: Optional[str] = None
    length_sleeve_raw: Optional[str] = None
    approx: list[str] = Field(default_factory=list)  # 예: "material:플리스". 1차 검색에 쓰이지 않고 표시만


class ProductMatch(_AttributesOut):
    product_id: str
    name: Optional[str] = None
    brand: Literal["Musinsa"] = "Musinsa"
    image_url: Optional[str] = None
    source: Literal["otpensource", "musinsa_snap"]
    kind: Literal["catalog_item", "snap_garment"]
    sub_colors: list[Color] = Field(default_factory=list)  # 표시만
    match: MatchResult = Field(default_factory=MatchResult)
    score: float = 0.0  # base_score + bonus
    score_breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    # v1.1 — 이미지(그래프 Product와 같은 이름·뜻). has_image = ProductNode.has_image
    gender: Optional[Gender] = None
    snap_id: Optional[int] = None
    image_ref: Optional[str] = None
    image_scope: Optional[ImageScope] = None
    image_status: Optional[ImageStatus] = None
    parent_id: Optional[str] = None
    multi_wearer_suspected: Optional[bool] = None
    has_image: bool = False


class TrendItem(_AttributesOut):
    id: str
    name: str
    styling_raw: Optional[str] = None
    styling_accessory: Optional[str] = None
    gender: Optional[Gender] = None
    request_match: MatchResult = Field(default_factory=MatchResult)
    image_urls: list[str] = Field(default_factory=list)  # Vogue 이미지 URL — 결과물에는 링크로만(G0-⑤: 임베드·핫링크·다운로드 금지)
    evidence_ids: list[str] = Field(default_factory=list)
    similar_products: list[ProductMatch] = Field(default_factory=list)
    # v1.1
    virtual: bool = False  # 가상 트렌드의 Item (id = virtual_item_id())
    item_focus: float = 1.0  # 트렌드 Top-K에서 이 Item의 비중 (코드가 등급 → 수치로 만든다, weights.yaml tier4.item_focus)
    value_weights: dict[str, dict[str, float]] = Field(default_factory=dict)  # {속성: {값: 가중치}} 코드가 등급으로 만든 값. 비면 모두 1.0


class Trend(BaseModel):
    style: TrendStyle
    items: list[TrendItem] = Field(default_factory=list)
    # v1.1 — 이 트렌드를 낸 용어와 판정 방식
    term: Optional[str] = None
    match_mode: Optional[MatchMode] = None
    qualification: Optional[Qualification] = None
    similarity: Optional[float] = None  # Tier3 코사인 유사도


class ResolutionStep(BaseModel):
    """Resolver가 시도한 Tier별 판정 사유 (시도 순서대로). code 목록은 문서 §2.3."""

    tier: Literal[1, 2, 3, 4]
    outcome: Literal["hit", "miss", "skipped", "error"]
    code: str
    detail: Optional[str] = None
    best: Optional[float] = None    # Tier3 1위 유사도
    second: Optional[float] = None  # Tier3 2위(다른 트렌드 그룹) 유사도


class LLMTrace(BaseModel):
    """Tier4 LLM 호출 출처 (재현용). LLM은 등급만 냈고 수치는 코드가 만들었다."""

    backend: str
    model: Optional[str] = None
    prompt_version: str
    cache: Literal["hit", "miss"]
    context_sha: str
    evidence: Literal["provided", "none"]  # 기사 근거 블록을 줬는가 ("근거 없음")


class TrendPick(ProductMatch):
    """트렌드 단위 Top-K 한 줄 = 상품 + 출처 Item.

    중복 제거 키(앞에서부터): snap_id(같은 사진) → parent_id(같은 세트) → dup_of(원 id로 묶음) → product_id.
    같은 키에서는 trend_score가 가장 높은 한 줄만 남기고, 나머지 출처 Item은 also_from_items에 적는다.
    """

    rank: int
    trend_score: float  # 그 Item에서의 score × item_focus (상품별 최고값)
    item_id: str
    item_name: str
    item_focus: float
    also_from_items: list[str] = Field(default_factory=list)


class TermResolution(BaseModel):
    """질의 용어 하나의 해석 결과 (Resolver Tier1~4)."""

    term: str
    norm: str  # graph_schema.normalize_term(term)
    status: Literal["resolved", "failed"]
    tier: Optional[Literal[1, 2, 3, 4]] = None
    match_mode: Optional[MatchMode] = None
    qualification: Optional[Qualification] = None
    similarity: Optional[float] = None
    steps: list[ResolutionStep] = Field(default_factory=list)
    style_ids: list[str] = Field(default_factory=list)  # trends[].style.id
    llm: Optional[LLMTrace] = None
    top_products: list[TrendPick] = Field(default_factory=list)


class AppliedSettings(BaseModel):
    """실제 적용한 점수·해석 설정 (재현용). weights_sha = configs/weights.yaml sha256 앞 12자."""

    weights_sha: str
    season_policy: Literal["strict", "report"]
    image_first_tiebreak: bool
    core_unknown_decay: float  # 실제 적용한 γ
    snap_id_limit: int
    item_gender_filter: bool
    value_weights: dict[str, float] = Field(default_factory=dict)
    tier3: Optional[dict] = None  # {"model", "tau", "delta"}
    top_k: int
    # 평가 실행 재현용 (T4 숨김 평가). 일반 실행에서는 기본값이다.
    hidden_style_groups: list[str] = Field(default_factory=list)  # 숨긴 name_norm 그룹
    tier4_context: Optional[Literal["none", "mentions"]] = None
    force_tier: Optional[Literal[1, 2, 3, 4]] = None


class EvidenceRecord(BaseModel):
    evidence_id: str  # v1.1: evidence_id(article_id, block_idx, quote)
    article_id: str
    title: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[str] = None
    block_idx: int
    quote: str


class TrendAnalysisResult(BaseModel):
    request_id: str
    # v1.1 partial = 상품을 낸 용어가 ≥1이고 해석 실패·상품 0인 용어도 ≥1 (문서 §2.1)
    status: Literal["ok", "partial", "no_results", "error"]
    applied_filters: Optional[AppliedFilters] = None
    trends: list[Trend] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    # v1.1
    schema_version: str = SCHEMA_VERSION
    resolutions: list[TermResolution] = Field(default_factory=list)
    applied_settings: Optional[AppliedSettings] = None
