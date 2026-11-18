"""트렌드 분석 최종 출력. 문서: docs/contracts.md

`schema_version`, 이미지 필드, Style `aliases`·`article_id`, 안정 evidence_id 규칙을 담는다.
선택 필드는 모두 기본값이 있다. `status`는 ok / no_results / error다.
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

SCHEMA_VERSION = "1.0.0"


def evidence_id(article_id: str, block_idx: int, quote: str) -> str:
    """안정 근거 ID `trend:<article_id>:<block_idx>:<hash8>` (hash8 = sha256(quote UTF-8) 앞 8자).

    같은 (기사, 블록, 인용)은 실행·질의가 달라도 같은 id다. quote는 저장된 문자열 그대로 해시한다(정규화하지 않는다).
    """
    return f"trend:{article_id}:{block_idx}:{hashlib.sha256(quote.encode('utf-8')).hexdigest()[:8]}"


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


class ExampleImage(BaseModel):
    """Style 예시 사진. 기사 이미지 URL이고, 결과를 쓰는 쪽은 링크로만 보여 준다(docs/data.md)."""
    url: str
    basis: Literal["item_link", "section", "article"]


class TrendStyle(BaseModel):
    id: str
    name: str
    name_origin: Literal["article", "analyst"]
    seasons: list[str] = Field(default_factory=list)  # 이 트렌드가 유행했다고 기사가 말한 시즌
    definition: str
    description: list[DescriptionOut] = Field(default_factory=list)
    focus_core: list[str] = Field(default_factory=list)
    focus_supporting: list[str] = Field(default_factory=list)
    focus_evidence_ids: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    article_id: Optional[str] = None
    # 추가 필드(기본값 빈 목록)
    example_images: list[ExampleImage] = Field(default_factory=list)


class MatchResult(BaseModel):
    matched: list[str] = Field(default_factory=list)
    mismatched: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)  # Item에 명시되지 않은 속성은 불일치가 아니다


class AttributeScore(BaseModel):
    weight: float  # 적용 가중치 = 기본 가중치 × 트렌드 배수
    base_weight: float
    level: Optional[Literal["core", "supporting"]] = None
    ratio: float  # 일치 비율 0~1 — 값 가중 재현율 Σ_{공통} w(v) ÷ Σ_{Item} w(v) (값 가중치가 없으면 |공통|/|Item|)


class ScoreBreakdown(BaseModel):
    attributes: dict[str, AttributeScore] = Field(default_factory=dict)
    base_score: float = 0.0  # Σ w·r ÷ Σ w
    bonus: dict[str, float] = Field(default_factory=dict)  # {"category_minor": α}
    unknown_core_weight: float = 0.0  # 항상 0.0 (예전 감쇠 옵션의 자리. 계약 필드라 남겨 둔다)
    value_weighted: bool = False      # 값 가중치를 적용했는가


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
    # 이미지(그래프 Product와 같은 이름·뜻). has_image = ProductNode.has_image
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
    image_urls: list[str] = Field(default_factory=list)  # Vogue 이미지 URL — 결과물에는 링크로만(임베드·핫링크·다운로드 금지)
    evidence_ids: list[str] = Field(default_factory=list)
    similar_products: list[ProductMatch] = Field(default_factory=list)


class Trend(BaseModel):
    style: TrendStyle
    items: list[TrendItem] = Field(default_factory=list)


class EvidenceRecord(BaseModel):
    evidence_id: str  # evidence_id(article_id, block_idx, quote)
    article_id: str
    title: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[str] = None
    block_idx: int
    quote: str


class TrendAnalysisResult(BaseModel):
    request_id: str
    status: Literal["ok", "no_results", "error"]
    applied_filters: Optional[AppliedFilters] = None
    trends: list[Trend] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
