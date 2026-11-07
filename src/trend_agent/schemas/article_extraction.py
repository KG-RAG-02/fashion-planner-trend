"""기사 추출 LLM 출력. 문서: docs/schemas/article_extraction.md

필드 이름은 문서와 같고, Item의 스칼라 필드는 graph_schema의 Item 속성과 같다.
`review_flags`는 LLM이 채우지 않고 코드(검증기)가 채운다.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .vocabulary import (
    DETAIL_ENUMS,
    MINOR_TO_MAJOR,
    CategoryMajor,
    CategoryMinor,
    Color,
    DetailType,
    FocusLevel,
    Fit,
    Gender,
    Material,
    Pattern,
    ScoredAttribute,
    Season,
    SeasonRef,
    Shape,
    BodyLength,
    SleeveLength,
)


class Evidence(BaseModel):
    block_idx: int
    quote: str


class DescriptionEntry(BaseModel):
    text: str
    evidence: list[Evidence] = Field(min_length=1)


class FocusAttribute(BaseModel):
    """이 트렌드에서 중요하게 봐야 할 속성 (수치 없음, 근거 필수)."""

    attribute: ScoredAttribute
    level: FocusLevel
    evidence: list[Evidence] = Field(min_length=1)


class ExtractedColor(BaseModel):
    raw: str
    value: Optional[Color] = None


class ExtractedMaterial(BaseModel):
    raw: str
    value: Optional[Material] = None


class ExtractedPattern(BaseModel):
    raw: str
    value: Optional[Pattern] = None


class ExtractedDetail(BaseModel):
    raw: str
    type: DetailType
    value: Optional[str] = None
    motif: Optional[str] = None
    placement: Optional[str] = None

    @model_validator(mode="after")
    def _value_in_type_enum(self) -> "ExtractedDetail":
        if self.value is not None:
            allowed = {m.value for m in DETAIL_ENUMS[self.type]}
            if self.value not in allowed:
                raise ValueError(f"{self.value!r} is not a valid {self.type.value} value")
        return self


class ImageLink(BaseModel):
    image_idx: int  # v1.1: 크롤러 images[].image_idx (기사 안 이미지 순번, 0부터). block idx가 아니다
    basis: Literal["caption_brand", "adjacent", "order"]


class ExtractedItem(BaseModel):
    name: str
    raw_text: str
    gender: Optional[Gender] = None
    size: None = None  # 항상 None. 핏은 fit.

    category_raw: Optional[str] = None
    category_major: Optional[CategoryMajor] = None
    category_minor: Optional[CategoryMinor] = None

    colors: list[ExtractedColor] = Field(default_factory=list)
    materials: list[ExtractedMaterial] = Field(default_factory=list)
    patterns: list[ExtractedPattern] = Field(default_factory=list)
    details: list[ExtractedDetail] = Field(default_factory=list)

    styling_raw: Optional[str] = None
    styling_accessory: Optional[str] = None

    fit_raw: Optional[str] = None
    fit: Optional[Fit] = None
    shape_raw: Optional[str] = None
    shape: Optional[Shape] = None  # 저장만, 1차 검색에 쓰지 않는다

    length_body_raw: Optional[str] = None
    length_body: Optional[BodyLength] = None
    length_sleeve_raw: Optional[str] = None
    length_sleeve: Optional[SleeveLength] = None

    evidence: list[Evidence] = Field(default_factory=list)
    image_links: list[ImageLink] = Field(default_factory=list)

    review_flags: list[str] = Field(default_factory=list)  # 코드가 채움

    @model_validator(mode="after")
    def _derive_major(self) -> "ExtractedItem":
        """category_minor가 있으면 category_major는 표로 정한다. LLM 값과 다르면 코드 값을 쓰고 표시한다."""
        if self.category_minor is not None:
            expected = MINOR_TO_MAJOR[self.category_minor]
            if self.category_major is not None and self.category_major != expected:
                self.review_flags.append(
                    f"category_major_mismatch:{self.category_major.value}->{expected.value}"
                )
            self.category_major = expected
        return self

    @property
    def in_graph_scope(self) -> bool:
        """category_major가 4개 중 하나인 Item만 그래프에 적재·탐색한다."""
        return self.category_major is not None


class ExtractedStyle(BaseModel):
    name: str
    name_origin: Literal["article", "analyst"]
    aliases: list[str] = Field(default_factory=list)  # v1.1: 기사 안의 다른 이름(영문 병기·번역·약칭), 원문 그대로. 없으면 []
    seasons: list[SeasonRef] = Field(default_factory=list)  # 기사가 그 트렌드가 유행했다고 말한 시즌 (명시된 것만)
    definition: str
    description: list[DescriptionEntry] = Field(default_factory=list)
    focus_attributes: list[FocusAttribute] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    items: list[ExtractedItem] = Field(default_factory=list)

    @field_validator("seasons")
    @classmethod
    def _unique_seasons(cls, v: list[SeasonRef]) -> list[SeasonRef]:
        keys = [x.key for x in v]
        if len(set(keys)) != len(keys):
            raise ValueError("seasons must not repeat")
        return v

    @field_validator("focus_attributes")
    @classmethod
    def _focus_rules(cls, v: list[FocusAttribute]) -> list[FocusAttribute]:
        attrs = [f.attribute for f in v]
        if len(set(attrs)) != len(attrs):
            raise ValueError("focus_attributes must not repeat an attribute")
        if sum(1 for f in v if f.level == FocusLevel.CORE) > 3:
            raise ValueError("at most 3 core focus attributes")
        return v


class ArticleExtraction(BaseModel):
    article_id: str
    styles: list[ExtractedStyle] = Field(default_factory=list)
