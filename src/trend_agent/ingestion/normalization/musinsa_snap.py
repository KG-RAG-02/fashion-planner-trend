"""03 `disl/musinsa-snap-new` → ProductRecord.

문서: docs/schemas/graph_schema.md §2.3, docs/schemas/sources/alias_map.md (§4.2 카테고리, §8 속성 간 이동, §9 핏)
- 03은 착장 사진 한 장에 상의·하의·아우터 라벨이 묶인 데이터다. 이 모듈은 **라벨 한 슬롯 = Product 하나**로 바꾼다.
- 색은 `서브컬러`뿐이라 `colors`는 비고 `sub_colors`만 채운다. 상품명·URL·브랜드·가격이 없다 (이미지는 parquet 바이트라 저장하지 않는다).
- 02와 같은 `AliasMap`을 쓴다 (`otpensource.py`와 같은 표준·같은 별칭 사전).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import AttrEdge, DetailEdge, ProductNode, ProductRecord

from .alias_map import AliasMap

REPO_ID = "disl/musinsa-snap-new"
SLOT_MAJOR = {"상의": V.CategoryMajor.UPPER, "하의": V.CategoryMajor.LOWER, "아우터": V.CategoryMajor.OUTER}
# 카테고리 값이 의류가 아닌 경우: 상분류를 정하지 않는다 (그래프 밖)
NON_GARMENT = {"모자", "스카프", "가방", "부츠", "백팩", "헤드웨어", "야구모자", "햇모자"}
_EMPTY = {"", "0", "nan", "none"}


def find_snapshot_dir() -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id=REPO_ID, repo_type="dataset"))


def _clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in _EMPTY else s


def parse_label(raw: str) -> dict[str, Any]:
    """label 열(JSON 문자열, 일부 값은 파이썬 dict 문자열)을 dict로."""
    outer = json.loads(raw)
    out: dict[str, Any] = {}
    for k, v in outer.items():
        if isinstance(v, str) and v.startswith("{"):
            v = ast.literal_eval(v)
        out[k] = v
    return out


def iter_slots(snapshot_dir: Path | str) -> Iterable[dict[str, Any]]:
    """착장 사진의 라벨 슬롯을 하나씩 낸다: {file_no, slot, attrs, k}. 슬롯 값이 "0"이면(해당 아이템 없음) 건너뛴다.
    같은 슬롯에 dict가 여러 개면(튜플) k = 0, 1, ...
    """
    import pandas as pd

    parquet = sorted(Path(snapshot_dir).rglob("*.parquet"))[0]
    df = pd.read_parquet(parquet, columns=["label"])  # image 바이트는 읽지 않는다
    for row_no, raw in enumerate(df["label"]):
        parsed = parse_label(raw)
        file_no = next((str(v) for k, v in parsed.items() if k.endswith("파일 번호")), f"row{row_no}")
        for key, val in parsed.items():
            if ".라벨링." not in key:
                continue
            slot = key.rsplit(".", 1)[-1]
            vals = list(val) if isinstance(val, tuple) else [val]
            for k, attrs in enumerate(vals):
                if isinstance(attrs, dict):
                    yield {"file_no": file_no, "slot": slot, "attrs": attrs, "k": k, "row": row_no}


class SnapNormalizer:
    def __init__(self, alias: Optional[AliasMap] = None):
        self.alias = alias or AliasMap()

    def normalize_slot(self, slot_row: dict[str, Any]) -> ProductRecord:
        d = slot_row["attrs"]
        slot = slot_row["slot"]
        approx_fields: list[str] = []

        # 카테고리: 하분류가 정해지면 상분류는 표에서 도출 (슬롯 이름보다 우선), 아니면 슬롯 이름
        cat_raw = _clean(d.get("카테고리"))
        n = self.alias.normalize("category", cat_raw)
        minor: Optional[str] = n.value
        if minor:
            major = V.MINOR_TO_MAJOR[V.CategoryMinor(minor)]
        elif cat_raw in NON_GARMENT:
            major = None
        else:
            major = SLOT_MAJOR.get(slot)

        materials: list[AttrEdge] = []
        patterns: list[AttrEdge] = []
        details: list[DetailEdge] = []
        length_sleeve: Optional[str] = None
        length_sleeve_raw: Optional[str] = _clean(d.get("소매기장")) or None
        shape_raw: Optional[str] = None
        shape: Optional[str] = None

        def add(lst: list, edge: Any) -> None:
            key = (getattr(edge, "type", None), edge.value)
            if edge.value and not any((getattr(e, "type", None), e.value) == key for e in lst):
                lst.append(edge)

        def add_detail(type_: V.DetailType, value: Optional[str], raw: str, approx: bool) -> None:
            if value:
                add(details, DetailEdge(type=type_, value=value, raw=raw, approx=approx))

        # 소재 (다운·패딩 힌트는 category_minor, 퀼팅 등은 Detail로)
        for t in self.alias.normalize_tokens("material", d.get("소재")):
            if t.hint.get("category_minor") and minor is None:
                minor = t.hint["category_minor"]
                major = V.MINOR_TO_MAJOR[V.CategoryMinor(minor)]
            elif t.route_to == "detail":
                add_detail(V.DetailType.DETAIL, t.value, t.raw, t.approx)
            elif t.value and t.route_to is None:
                add(materials, AttrEdge(value=t.value, raw=t.raw, approx=t.approx))

        # 프린트 → Pattern (데님은 Material, 플리츠·패치워크·자수는 Detail)
        for t in self.alias.normalize_tokens("pattern", d.get("프린트")):
            if t.route_to == "material" and t.value:
                add(materials, AttrEdge(value=t.value, raw=t.raw, approx=t.approx))
            elif t.route_to == "detail":
                add_detail(V.DetailType.DETAIL, t.value, t.raw, t.approx)
            elif t.value and t.route_to is None:
                add(patterns, AttrEdge(value=t.value, raw=t.raw, approx=t.approx))

        # 디테일 (프린트·스트라이프 등은 Pattern, 민소매는 소매 길이, 홀터넥·터틀넥은 Neckline으로 이동)
        for t in self.alias.normalize_tokens("detail", d.get("디테일")):
            if t.route_to == "pattern":
                add(patterns, AttrEdge(value=t.value, raw=t.raw, approx=t.approx)) if t.value else None
            elif t.route_to == "sleeve_length":
                length_sleeve, length_sleeve_raw = t.value, t.raw
            elif t.route_to == "neckline":
                add_detail(V.DetailType.NECKLINE, t.value, t.raw, t.approx)
            elif t.route_to is None:
                add_detail(V.DetailType.DETAIL, t.value, t.raw, t.approx)

        # 넥라인 · 칼라 (서로 잘못 들어온 값은 맞는 쪽으로)
        for t in self.alias.normalize_tokens("neckline", d.get("넥라인")):
            if t.route_to == "collar":
                add_detail(V.DetailType.COLLAR, t.value, t.raw, t.approx)
            elif t.route_to is None:
                add_detail(V.DetailType.NECKLINE, t.value, t.raw, t.approx)
        for t in self.alias.normalize_tokens("collar", d.get("칼라")):
            if t.route_to == "neckline":
                add_detail(V.DetailType.NECKLINE, t.value, t.raw, t.approx)
            elif t.route_to is None:
                add_detail(V.DetailType.COLLAR, t.value, t.raw, t.approx)

        # 핏 (Shape 값이 들어온 경우는 shape로, 슬림→타이트는 approx)
        fit_raw = _clean(d.get("핏")) or None
        fit: Optional[str] = None
        if fit_raw:
            f = self.alias.normalize("fit", fit_raw)
            if f.route_to == "shape":
                shape_raw, shape = fit_raw, f.value
            elif f.value:
                fit = f.value
                if f.approx:
                    approx_fields.append("fit")

        # 기장 · 소매기장
        length_body_raw = _clean(d.get("기장")) or None
        length_body: Optional[str] = None
        if length_body_raw:
            b = self.alias.normalize("body_length", length_body_raw)
            length_body = b.value
            if b.approx:
                approx_fields.append("length_body")
        if length_sleeve is None and length_sleeve_raw:
            s = self.alias.normalize("sleeve_length", length_sleeve_raw)
            length_sleeve = s.value
            if s.approx:
                approx_fields.append("length_sleeve")

        # 서브컬러 (점수·필터에 쓰지 않는다)
        sub_colors: list[str] = []
        for c in self.alias.normalize_color(d.get("서브컬러")):
            if c.value and c.value not in sub_colors:
                sub_colors.append(c.value)

        suffix = f":{slot_row['k']}" if slot_row["k"] else ""
        node = ProductNode(
            id=f"musinsa_snap:{slot_row['row']}:{slot}{suffix}",  # 파일 번호는 중복되고 내용이 다른 행이 있어(약 600슬롯) 행 번호를 쓴다
            source="musinsa_snap",
            kind="snap_garment",
            category_raw=cat_raw or None,
            category_minor=minor,
            fit_raw=fit_raw,
            fit=fit,
            shape_raw=shape_raw,
            shape=shape,
            length_body_raw=length_body_raw,
            length_body=length_body,
            length_sleeve_raw=length_sleeve_raw,
            length_sleeve=length_sleeve,
            sub_colors=sub_colors,
            source_row=f"{REPO_ID}#row{slot_row['row']}:{slot}{suffix} (file {slot_row['file_no']})",
            approx=approx_fields,
        )
        return ProductRecord(node=node, category_major=major, colors=[], materials=materials, patterns=patterns, details=details)


def snap_report(records: list[ProductRecord]) -> dict[str, Any]:
    from collections import Counter

    n = len(records)
    return {
        "slots": n,
        "category_minor": sum(1 for r in records if r.node.category_minor),
        "category_major_only": sum(1 for r in records if r.category_major and not r.node.category_minor),
        "category_none": sum(1 for r in records if r.category_major is None),
        "sub_color_rows": sum(1 for r in records if r.node.sub_colors),
        "material_rows": sum(1 for r in records if r.materials),
        "pattern_rows": sum(1 for r in records if r.patterns),
        "detail_rows": sum(1 for r in records if r.details),
        "fit_rows": sum(1 for r in records if r.node.fit),
        "fit_approx_rows": sum(1 for r in records if "fit" in r.node.approx),
        "length_body_rows": sum(1 for r in records if r.node.length_body),
        "length_sleeve_rows": sum(1 for r in records if r.node.length_sleeve),
        "by_category_major": dict(Counter(r.category_major.value for r in records if r.category_major)),
        "by_category_minor": dict(Counter(r.node.category_minor for r in records if r.node.category_minor).most_common()),
        "fit_values": dict(Counter(r.node.fit for r in records if r.node.fit).most_common()),
    }
