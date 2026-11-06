"""트렌드 분석 최종 출력. 문서: docs/schemas/trend_analysis_result.md"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

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
    name_origin: Literal["article", "analyst"]
    seasons: list[str] = Field(default_factory=list)  # 이 트렌드가 유행했다고 기사가 말한 시즌
    definition: str
    description: list[DescriptionOut] = Field(default_factory=list)
    focus_core: list[str] = Field(default_factory=list)
    focus_supporting: list[str] = Field(default_factory=list)
    focus_evidence_ids: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    matched: list[str] = Field(default_factory=list)
    mismatched: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)  # Item에 명시되지 않은 속성은 불일치가 아니다


class AttributeScore(BaseModel):
    weight: float  # 적용 가중치 = 기본 가중치 × 트렌드 배수
    base_weight: float
    level: Optional[Literal["core", "supporting"]] = None
    ratio: float  # 일치 비율 0~1


class ScoreBreakdown(BaseModel):
    attributes: dict[str, AttributeScore] = Field(default_factory=dict)
    base_score: float = 0.0
    bonus: dict[str, float] = Field(default_factory=dict)  # {"category_minor": α}


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


class TrendItem(_AttributesOut):
    id: str
    name: str
    styling_raw: Optional[str] = None
    styling_accessory: Optional[str] = None
    gender: Optional[Gender] = None
    request_match: MatchResult = Field(default_factory=MatchResult)
    image_urls: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    similar_products: list[ProductMatch] = Field(default_factory=list)


class Trend(BaseModel):
    style: TrendStyle
    items: list[TrendItem] = Field(default_factory=list)


class EvidenceRecord(BaseModel):
    evidence_id: str
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
