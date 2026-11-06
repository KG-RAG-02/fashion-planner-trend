"""Neo4j에 저장되는 노드·관계 속성. 문서: docs/schemas/graph_schema.md

Neo4j 속성은 원시 값과 그 배열만 가질 수 있다. 이 모듈의 *Node 모델은 평탄한 속성만 갖고,
탐색 경로로 쓰는 값(색·소재·패턴·디테일·상분류)은 *Record의 관계 목록에 둔다.
"""
from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .article_extraction import ExtractedItem, ExtractedStyle
from .vocabulary import (
    BodyLength,
    CategoryMajor,
    CategoryMinor,
    Color,
    DetailType,
    FocusLevel,
    Fit,
    Gender,
    Season,
    Shape,
    SleeveLength,
)

SCORED_ATTRIBUTES = ("color", "material", "pattern", "detail", "fit")


# ---------- 관계 ----------
class AttrEdge(BaseModel):
    """HAS_COLOR / HAS_MATERIAL / HAS_PATTERN {raw, approx}. value는 표준 값 = 노드 id."""

    value: str
    raw: Optional[str] = None
    approx: bool = False


class DetailEdge(BaseModel):
    """HAS_DETAIL {raw, motif, placement, approx}. 노드 id는 "<type>:<value>"."""

    type: DetailType
    value: str
    raw: Optional[str] = None
    motif: Optional[str] = None
    placement: Optional[str] = None
    approx: bool = False


# ---------- 노드 ----------
class StyleNode(BaseModel):
    id: str
    name: str
    name_origin: Literal["article", "analyst"]
    seasons: list[str] = Field(default_factory=list)  # 예: ["2026FW", "2027SS"]
    definition: str
    description: list[str] = Field(default_factory=list)
    focus_core: list[str] = Field(default_factory=list)
    focus_supporting: list[str] = Field(default_factory=list)
    # 아래는 코드가 계산 (추출값 아님). 1차에서는 저장·확인용이고 가중치에 쓰지 않는다.
    item_count: int = 0
    coverage_color: Optional[float] = None
    coverage_material: Optional[float] = None
    coverage_pattern: Optional[float] = None
    coverage_detail: Optional[float] = None
    coverage_fit: Optional[float] = None
    concentration_color: Optional[float] = None
    concentration_material: Optional[float] = None
    concentration_pattern: Optional[float] = None
    concentration_detail: Optional[float] = None
    concentration_fit: Optional[float] = None


class ItemNode(BaseModel):
    id: str
    name: str
    raw_text: str
    gender: Optional[Gender] = None
    size: None = None
    category_raw: Optional[str] = None
    category_minor: Optional[CategoryMinor] = None
    fit_raw: Optional[str] = None
    fit: Optional[Fit] = None
    shape_raw: Optional[str] = None
    shape: Optional[Shape] = None
    length_body_raw: Optional[str] = None
    length_body: Optional[BodyLength] = None
    length_sleeve_raw: Optional[str] = None
    length_sleeve: Optional[SleeveLength] = None
    styling_raw: Optional[str] = None
    styling_accessory: Optional[str] = None
    image_urls: list[str] = Field(default_factory=list)
    image_basis: list[str] = Field(default_factory=list)
    article_id: str
    approx: list[str] = Field(default_factory=list)  # 근사 매핑으로 채운 스칼라 필드 이름 (예: "fit")


class ProductNode(BaseModel):
    id: str
    name: Optional[str] = None
    image_url: Optional[str] = None
    source: Literal["otpensource", "musinsa_snap"]
    kind: Literal["catalog_item", "snap_garment"]
    gender: Optional[Gender] = None
    season: Optional[Season] = None
    size: None = None
    category_raw: Optional[str] = None
    category_minor: Optional[CategoryMinor] = None
    fit_raw: Optional[str] = None
    fit: Optional[Fit] = None
    shape_raw: Optional[str] = None
    shape: Optional[Shape] = None
    length_body_raw: Optional[str] = None
    length_body: Optional[BodyLength] = None
    length_sleeve_raw: Optional[str] = None
    length_sleeve: Optional[SleeveLength] = None
    sub_colors: list[Color] = Field(default_factory=list)  # 점수·필터에 쓰지 않는다
    product_url: None = None
    price: None = None
    source_row: str
    approx: list[str] = Field(default_factory=list)  # 근사 매핑으로 채운 스칼라 필드 이름 (예: "fit")


# ---------- 노드 + 관계 묶음 ----------
class _Record(BaseModel):
    category_major: Optional[CategoryMajor] = None  # IN_CATEGORY (하나만)
    colors: list[AttrEdge] = Field(default_factory=list)
    materials: list[AttrEdge] = Field(default_factory=list)
    patterns: list[AttrEdge] = Field(default_factory=list)
    details: list[DetailEdge] = Field(default_factory=list)

    @property
    def in_graph_scope(self) -> bool:
        return self.category_major is not None

    def usable(self, attribute: str) -> list[Any]:
        """검색·점수에 쓸 수 있는 관계 값. 근사(approx) 값은 1차에서 쓰지 않는다 (미확인 취급)."""
        edges = {"color": self.colors, "material": self.materials, "pattern": self.patterns,
                 "detail": self.details}.get(attribute, [])
        return [e for e in edges if not e.approx]

    def usable_scalar(self, name: str) -> Any:
        """fit 같은 스칼라 속성. 근사 매핑으로 채운 값은 1차 검색에서 쓰지 않는다 (None 취급)."""
        if name in self.node.approx:
            return None
        return getattr(self.node, name, None)


class ItemRecord(_Record):
    node: ItemNode


class ProductRecord(_Record):
    node: ProductNode


# ---------- 변환 ----------
def style_node_from_extraction(style: ExtractedStyle, style_id: str) -> StyleNode:
    return StyleNode(
        id=style_id,
        name=style.name,
        name_origin=style.name_origin,
        seasons=[x.key for x in style.seasons],
        definition=style.definition,
        description=[d.text for d in style.description],
        focus_core=[f.attribute.value for f in style.focus_attributes if f.level == FocusLevel.CORE],
        focus_supporting=[f.attribute.value for f in style.focus_attributes if f.level == FocusLevel.SUPPORTING],
    )


def item_record_from_extraction(
    item: ExtractedItem,
    item_id: str,
    article_id: str,
    image_urls: Optional[dict[int, str]] = None,
) -> ItemRecord:
    """ExtractedItem → ItemRecord. 스칼라 필드 이름은 같아서 이름을 바꾸지 않는다.

    값이 None인 표준 값 관계는 만들지 않는다. image_urls는 {image_idx: url}.
    """
    urls = image_urls or {}
    links = [l for l in item.image_links if l.image_idx in urls]
    node = ItemNode(
        id=item_id,
        article_id=article_id,
        image_urls=[urls[l.image_idx] for l in links],
        image_basis=[l.basis for l in links],
        **item.model_dump(
            include={
                "name", "raw_text", "gender", "category_raw", "category_minor",
                "fit_raw", "fit", "shape_raw", "shape",
                "length_body_raw", "length_body", "length_sleeve_raw", "length_sleeve",
                "styling_raw", "styling_accessory",
            }
        ),
    )
    return ItemRecord(
        node=node,
        category_major=item.category_major,
        colors=[AttrEdge(value=c.value.value, raw=c.raw) for c in item.colors if c.value is not None],
        materials=[AttrEdge(value=m.value.value, raw=m.raw) for m in item.materials if m.value is not None],
        patterns=[AttrEdge(value=p.value.value, raw=p.raw) for p in item.patterns if p.value is not None],
        details=[
            DetailEdge(type=d.type, value=d.value, raw=d.raw, motif=d.motif, placement=d.placement)
            for d in item.details if d.value is not None
        ],
    )


def _attribute_values(rec: _Record, attribute: str) -> set[str]:
    if attribute == "fit":
        fit = getattr(rec.node, "fit", None)
        return {fit.value} if fit is not None else set()
    if attribute == "detail":
        return {f"{e.type.value}:{e.value}" for e in rec.details}
    edges = {"color": rec.colors, "material": rec.materials, "pattern": rec.patterns}[attribute]
    return {e.value for e in edges}


def compute_style_profile(items: list[ItemRecord]) -> dict[str, Any]:
    """Style의 Item들에서 coverage_*, concentration_*, item_count를 계산한다 (graph_schema.md §2.1).

    - 그래프 대상 Item(category_major 있음)만 센다.
    - coverage = 그 속성에 표준 값이 있는 Item 수 / Item 수. 같은 Item이 같은 값을 여러 번 가져도 한 번.
    - concentration = 값이 있는 Item 중 가장 많이 나온 값을 가진 Item 수 / 값이 있는 Item 수.
    - 근사(approx) 값도 저장용 프로파일에서는 센다 (확인용). 가중치에는 쓰지 않는다.
    """
    scoped = [i for i in items if i.in_graph_scope]
    profile: dict[str, Any] = {"item_count": len(scoped)}
    for attr in SCORED_ATTRIBUTES:
        if not scoped:
            profile[f"coverage_{attr}"] = None
            profile[f"concentration_{attr}"] = None
            continue
        per_item = [_attribute_values(i, attr) for i in scoped]
        filled = [v for v in per_item if v]
        profile[f"coverage_{attr}"] = round(len(filled) / len(scoped), 4)
        if not filled:
            profile[f"concentration_{attr}"] = None
        else:
            counts = Counter(value for values in filled for value in values)
            profile[f"concentration_{attr}"] = round(counts.most_common(1)[0][1] / len(filled), 4)
    return profile


def flatten(model: BaseModel) -> dict[str, Any]:
    """노드를 Neo4j 속성 dict로 만든다: Enum은 값으로, None 속성은 생략한다."""

    def conv(v: Any) -> Any:
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    return {k: conv(v) for k, v in model.model_dump().items() if v is not None}
