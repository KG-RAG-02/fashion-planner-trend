"""Neo4j에 저장되는 노드·관계 속성. 문서: docs/schemas/graph_schema.md (계약 v1.1)

Neo4j 속성은 원시 값과 그 배열만 가질 수 있다. 이 모듈의 *Node 모델은 평탄한 속성만 갖고,
탐색 경로로 쓰는 값(색·소재·패턴·디테일·상분류)은 *Record의 관계 목록에 둔다.

v1.1: Product 이미지·정제 필드, Style `aliases`·`name_norm`, Term 계층(`TermNode`·`ALIAS_OF`·`MAPS_TO`),
용어 정규화 `normalize_term()`(Style·Term·질의 공통, 코드 1곳). 추가 필드는 모두 Optional/기본값이라
v1.0 데이터는 그대로 유효하다.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

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

# ---------- v1.1 공통 어휘 ----------
ImageScope = Literal["catalog", "full_snap", "full_set"]  # 상품 컷 / 착장 원본 1장(Q4, 크롭 없음) / 세트 부모 이미지 공유
ImageStatus = Literal["ok", "broken", "unchecked"]        # 02는 스트리밍 GET, 03은 원본 디코드로만 정한다(HEAD 금지)
TermKind = Literal["alias", "translation", "trend_word"]
TermOrigin = Literal["label", "extraction", "term_mentions", "manual", "llm"]
ValueLevel = Literal["primary", "secondary"]

# 용어 정규화 (graph_schema.md §1.1). 바꾸면 Term id·Style name_norm·C1 Term 벡터 id가 모두 바뀌므로 계약 변경이다.
_NORM_DROP = re.compile(r"[\s\-_·•'\"‘’“”`.,()\[\]{}:;/!?]+")
_NORM_SUFFIXES = ("fashion", "style", "trend", "look", "스타일", "트렌드", "패션", "코디", "룩")  # 긴 것부터, 1회만


def normalize_term(text: str) -> str:
    """트렌드어·Style명·별칭의 비교 키: NFKC → 소문자 → 공백·구두점 제거 → 끝의 일반어 1회 제거(남는 길이 ≥2일 때).

    예: "‘절제된 맥시멀리즘’" → "절제된맥시멀리즘", "발레코어 룩" → "발레코어", "Quiet Luxury Style" → "quietluxury".
    """
    s = _NORM_DROP.sub("", unicodedata.normalize("NFKC", text or "").lower())
    for suffix in _NORM_SUFFIXES:
        if s.endswith(suffix) and len(s) - len(suffix) >= 2:
            return s[: -len(suffix)]
    return s


def term_id(norm: str) -> str:
    """Term 노드 id = "term:<norm>" (C1 Term 벡터 id도 같다)."""
    return f"term:{norm}"


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
    # v1.1 — 이름 일치(Tier1)와 Term 재구성용. name_norm·aliases_norm은 코드가 normalize_term()으로 계산한다.
    aliases: list[str] = Field(default_factory=list)  # 기사 안의 다른 이름(원문 그대로). 원천: 추출 styles[].aliases
    article_id: Optional[str] = None
    name_norm: Optional[str] = None
    aliases_norm: list[str] = Field(default_factory=list)


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
    # v1.1 — 이미지(원천: C2 이미지 인덱스)와 정제 표시(원천: T2). 모두 Optional이고 None이면 flatten()이
    # 속성을 만들지 않으므로 v1 데이터는 v1.0 그래프와 같다. bool은 해당할 때만 True로 채운다.
    snap_id: Optional[int] = None                      # 03 스냅 사진 = HF parquet 0-based row (같은 사진의 의류는 같은 값)
    image_ref: Optional[str] = None                    # 03 "hf://datasets/disl/musinsa-snap-new@<rev>#row=<row>" · 02 image_url
    image_scope: Optional[ImageScope] = None
    image_status: Optional[ImageStatus] = None         # 이미지 참조가 없으면 None
    multi_wearer_suspected: Optional[bool] = None
    parent_id: Optional[str] = None                    # 02 세트 분해 구성품의 원 상품 id (구성품 id = "<원 id>#<k>")
    split_from: Optional[Literal["name_rule"]] = None   # 분해 근거
    dup_of: Optional[str] = None                       # 같은 내용으로 의심되는 먼저 나온 Product id (표시만, 지우지 않는다)
    multipack: Optional[bool] = None                   # 같은 종류 N개 묶음 (분해하지 않음)
    split_candidate: Optional[bool] = None             # 세트 표지는 있으나 근거 부족으로 분해하지 않은 행 (사람 검토)

    @property
    def has_image(self) -> bool:
        """결과 표시·동점 정렬에 쓰는 단일 정의 (저장하지 않는다): status가 있으면 == "ok", 없으면 참조 존재."""
        if self.image_status is not None:
            return self.image_status == "ok"
        return bool(self.image_url or self.image_ref)


class TermNode(BaseModel):
    """(:Term) — 트렌드어·별칭·번역. 원본은 $SHARED/terms/term_map.jsonl(T3)이고 적재할 때마다 다시 만든다."""

    id: str                                            # term_id(norm)
    text: str                                          # 대표 표기
    norm: str                                          # normalize_term(text)
    kind: TermKind
    origin: TermOrigin
    article_count: Optional[int] = None                # 언급 기사 수 (표시용, T1 term_mentions)
    profile_json: Optional[str] = None                 # Tier4 프로필(JSON 문자열 — Neo4j는 중첩 속성 불가)
    profile_backend: Optional[str] = None
    profile_model: Optional[str] = None
    profile_prompt_version: Optional[str] = None

    @model_validator(mode="after")
    def _consistent_keys(self) -> "TermNode":
        if self.norm != normalize_term(self.text):
            raise ValueError(f"norm {self.norm!r} != normalize_term(text) {normalize_term(self.text)!r}")
        if self.id != term_id(self.norm):
            raise ValueError(f"id must be {term_id(self.norm)!r}")
        return self


class AliasOfEdge(BaseModel):
    """(Term)-[:ALIAS_OF {origin, article_id}]->(Style). Style을 병합하지 않고 잇는다."""

    origin: TermOrigin
    article_id: Optional[str] = None


class MapsToEdge(BaseModel):
    """(Term)-[:MAPS_TO {...}]->(Color|Material|Pattern|Detail|Category). Tier4 프로필 중 노드형 속성만.

    `value`는 대상 노드 id (Detail은 "<type>:<name>"). fit·하분류는 노드가 아니므로 TermNode.profile_json에 둔다.
    수치(가중치)는 저장하지 않는다 — 등급만 두고 수치화는 코드(weights.yaml)가 한다.
    """

    attribute: Literal["color", "material", "pattern", "detail", "category"]
    value: str
    level: Optional[FocusLevel] = None                 # core | supporting
    value_level: Optional[ValueLevel] = None           # primary | secondary
    item_idx: Optional[int] = None
    origin: TermOrigin = "llm"
    model: Optional[str] = None
    prompt_version: Optional[str] = None


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
def style_node_from_extraction(style: ExtractedStyle, style_id: str, article_id: Optional[str] = None) -> StyleNode:
    """ExtractedStyle → StyleNode. `name_norm`·`aliases_norm`은 normalize_term()으로 계산한다 (v1.1).

    article_id를 주지 않으면 Style id 규칙(`<article_id>-s<n>`)에서 읽는다.
    """
    if article_id is None and "-s" in style_id:
        article_id = style_id.rsplit("-s", 1)[0]
    aliases = list(dict.fromkeys(a for a in style.aliases if a and a.strip()))
    name_norm = normalize_term(style.name)
    return StyleNode(
        id=style_id,
        name=style.name,
        name_origin=style.name_origin,
        seasons=[x.key for x in style.seasons],
        definition=style.definition,
        description=[d.text for d in style.description],
        focus_core=[f.attribute.value for f in style.focus_attributes if f.level == FocusLevel.CORE],
        focus_supporting=[f.attribute.value for f in style.focus_attributes if f.level == FocusLevel.SUPPORTING],
        aliases=aliases,
        article_id=article_id,
        name_norm=name_norm,
        aliases_norm=[n for n in dict.fromkeys(normalize_term(a) for a in aliases) if n and n != name_norm],
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
