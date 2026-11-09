"""질의 전처리 출력. 문서: docs/schemas/trend_query.md"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .vocabulary import (
    MINOR_TO_MAJOR,
    CategoryMajor,
    CategoryMinor,
    Collar,
    Color,
    DetailName,
    Fit,
    Gender,
    Material,
    Neckline,
    Pattern,
    Season,
)


def next_fashion_season(today: date) -> tuple[int, Season]:
    """질의에 시즌이 없을 때 쓰는 "다음 패션 시즌". 날짜는 코드가 계산한다 (LLM이 추측하지 않는다).

    3~8월 → 같은 해 FW, 9~12월 → 다음 해 SS, 1~2월 → 같은 해 SS.
    """
    if 3 <= today.month <= 8:
        return today.year, Season.FW
    if today.month >= 9:
        return today.year + 1, Season.SS
    return today.year, Season.SS


def style_matches_season(style_seasons: list[str], request: "SeasonRequest") -> bool:
    """Style이 요청 시즌에 유행했다고 기사가 말했는가. 시즌이 비어 있는(미확인) Style은 False다.

    미확인 Style은 시즌 조건 검색에서 보류하고, 응답의 limitations에 개수를 알린다.
    """
    return f"{request.year}{request.season.value}" in style_seasons


class SeasonRequest(BaseModel):
    year: int
    season: Season
    raw: Optional[str] = None
    origin: Literal["user_requested", "default_next"]

    @classmethod
    def default_next(cls, today: date) -> "SeasonRequest":
        year, season = next_fashion_season(today)
        return cls(year=year, season=season, raw=None, origin="default_next")


class TermRef(BaseModel):
    raw: str
    value: Optional[str] = None  # 트렌드명·상품 어휘는 아직 없다


class TrendTerms(BaseModel):
    season: SeasonRequest
    style_names: list[TermRef] = Field(default_factory=list)
    item_terms: list[TermRef] = Field(default_factory=list)


class CategoryRef(BaseModel):
    raw: str
    major: Optional[CategoryMajor] = None
    minor: Optional[CategoryMinor] = None

    @model_validator(mode="after")
    def _derive_major(self) -> "CategoryRef":
        if self.minor is not None:
            self.major = MINOR_TO_MAJOR[self.minor]
        return self


class CategoryFilter(BaseModel):
    include: list[CategoryRef] = Field(default_factory=list)  # 비면 상분류 4개 전체
    exclude: list[CategoryRef] = Field(default_factory=list)


class GenderRef(BaseModel):
    raw: Optional[str] = None
    value: Optional[Gender] = None


_ATTRIBUTE_VALUES: dict[str, set[str]] = {
    "color": {m.value for m in Color},
    "material": {m.value for m in Material},
    "pattern": {m.value for m in Pattern},
    "detail": {m.value for m in DetailName} | {m.value for m in Neckline} | {m.value for m in Collar},
    "fit": {m.value for m in Fit},
}


class RequestedAttribute(BaseModel):
    type: Literal["color", "material", "pattern", "detail", "fit"]
    raw: str
    value: Optional[str] = None

    @model_validator(mode="after")
    def _value_in_vocabulary(self) -> "RequestedAttribute":
        if self.value is not None and self.value not in _ATTRIBUTE_VALUES[self.type]:
            raise ValueError(f"{self.value!r} is not a valid {self.type} value")
        return self


class UnsupportedCondition(BaseModel):
    type: str
    raw: str
    reason: str


class Requested(BaseModel):
    categories: CategoryFilter = Field(default_factory=CategoryFilter)
    gender: Optional[GenderRef] = None
    attributes: list[RequestedAttribute] = Field(default_factory=list)


class TrendQuery(BaseModel):
    request_id: str
    original_query: str
    trend_terms: TrendTerms
    requested: Requested = Field(default_factory=Requested)
    unsupported: list[UnsupportedCondition] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
