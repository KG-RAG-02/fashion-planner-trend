"""03 `disl/musinsa-snap-new` → ProductRecord.

문서: docs/schemas/graph_schema.md §2.3, docs/schemas/sources/alias_map.md (§4.2 카테고리, §8 속성 간 이동, §9 핏)
- 03은 착장 사진 한 장에 상의·하의·아우터 라벨이 묶인 데이터다. 이 모듈은 **라벨 한 슬롯 = Product 하나**로 바꾼다.
- 색은 `서브컬러`뿐이라 `colors`는 비고 `sub_colors`만 채운다. 상품명·URL·브랜드·가격이 없다 (이미지는 parquet 바이트라 저장하지 않는다).
- 02와 같은 `AliasMap`을 쓴다 (`otpensource.py`와 같은 표준·같은 별칭 사전).

v2 (T2): (a) 노이즈 카테고리를 슬롯 상분류로 채우지 않는다(결정 G0-②) · (b) 리스트형 라벨 값 보존 ·
(c) 같은 사진 안 동일 라벨 `dup_of` · (d) `snap_id` = parquet row · (e) 패딩 힌트는 상분류가 비었거나 아우터일 때만.
"""
from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import AttrEdge, DetailEdge, ProductNode, ProductRecord

from .alias_map import AliasMap

REPO_ID = "disl/musinsa-snap-new"
REVISION = "4335d233ef3b54bc5132a4d572d589042a41219d"  # C2가 받은 고정 리비전($SHARED/hf/data)
# 카테고리 값이 의류가 아닌 경우: 상분류를 정하지 않는다 (그래프 밖). 사전 null_values에도 있다 — 리포트 분류용.
NON_GARMENT = {"모자", "스카프", "가방", "부츠", "백팩", "헤드웨어", "야구모자", "햇모자"}
SCALAR_FIELDS = ("카테고리", "핏", "기장", "소매기장")  # 값 하나만 쓰는 필드: 리스트가 오면 첫 원소(리포트에 센다)
_EMPTY = {"", "0", "nan", "none"}
_PAD_OK_MAJORS = (None, V.CategoryMajor.OUTER)


def find_snapshot_dir() -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id=REPO_ID, repo_type="dataset"))


def _clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in _EMPTY else s


def _scalar(v: Any) -> str:
    """스칼라 필드 값. 리스트·튜플이면 비지 않은 첫 원소(버그 (b): v1은 `str(list)`를 원문으로 썼다)."""
    if isinstance(v, (list, tuple)):
        return next((c for c in (_clean(x) for x in v) if c), "")
    return _clean(v)


def scalar_list_fields(attrs: dict[str, Any]) -> list[str]:
    """스칼라 필드인데 리스트로 온 필드 이름 (리포트용)."""
    return [k for k in SCALAR_FIELDS if isinstance(attrs.get(k), (list, tuple))]


def canonical_label(attrs: dict[str, Any]) -> str:
    """dup_of 비교용 원문 라벨: 값 정리(_clean, 빈 값·"0" 제거) 후 키 정렬 JSON."""
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if isinstance(v, (list, tuple)):
            vals = [c for c in (_clean(x) for x in v) if c]
            if vals:
                out[k] = vals
        else:
            c = _clean(v)
            if c:
                out[k] = c
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


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

        # 카테고리: 카테고리 값으로만 정한다(슬롯 이름은 쓰지 않는다, alias_map.md §4.2 · 결정 G0-②).
        # 하분류가 정해지면 상분류는 표에서 도출, 상분류 이름·하분류 모르는 의류명은 hint로 상분류만,
        # 나머지(노이즈·비의류·판단 보류)는 상분류·하분류 null → 그래프 적재 제외(category_raw는 보존).
        cat_raw = _scalar(d.get("카테고리"))
        n = self.alias.normalize("category", cat_raw)
        minor: Optional[str] = n.value
        major: Optional[V.CategoryMajor] = None
        if minor:
            major = V.MINOR_TO_MAJOR[V.CategoryMinor(minor)]
        elif n.matched_by == "hint" and n.hint.get("category_major"):
            major = V.CategoryMajor(n.hint["category_major"])

        materials: list[AttrEdge] = []
        patterns: list[AttrEdge] = []
        details: list[DetailEdge] = []
        length_sleeve: Optional[str] = None
        length_sleeve_raw: Optional[str] = _scalar(d.get("소매기장")) or None
        shape_raw: Optional[str] = None
        shape: Optional[str] = None

        def add(lst: list, edge: Any) -> None:
            key = (getattr(edge, "type", None), edge.value)
            if edge.value and not any((getattr(e, "type", None), e.value) == key for e in lst):
                lst.append(edge)

        def add_detail(type_: V.DetailType, value: Optional[str], raw: str, approx: bool) -> None:
            if value:
                add(details, DetailEdge(type=type_, value=value, raw=raw, approx=approx))

        # 소재 (다운·패딩 힌트는 상분류가 비었거나 아우터일 때만 category_minor, 상의·하의·원피스면 Detail 패딩 — 버그 (e))
        for t in self.alias.normalize_tokens("material", d.get("소재")):
            if t.hint.get("category_minor"):
                if minor is None and major in _PAD_OK_MAJORS:
                    minor = t.hint["category_minor"]
                    major = V.MINOR_TO_MAJOR[V.CategoryMinor(minor)]
                elif minor is None:
                    add_detail(V.DetailType.DETAIL, V.DetailName.PADDING.value, t.raw, False)
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
        fit_raw = _scalar(d.get("핏")) or None
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
        length_body_raw = _scalar(d.get("기장")) or None
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
            snap_id=int(slot_row["row"]),
        )
        return ProductRecord(node=node, category_major=major, colors=[], materials=materials, patterns=patterns, details=details)


def mark_snap_duplicates(records: list[ProductRecord], slots: list[dict[str, Any]]) -> list[ProductRecord]:
    """(c) 같은 row(사진) 안에서 원문 라벨(canonical_label)이 완전히 같은 뒤쪽 슬롯에 `dup_of` = 앞 슬롯 id.
    표시만 한다(지우거나 병합하지 않는다). 순서는 라벨 순서 × k. 값이 모두 빈 라벨은 표시하지 않는다."""
    first: dict[tuple[int, str], str] = {}
    for rec, slot in zip(records, slots):
        key = canonical_label(slot["attrs"])
        if key == "{}":
            continue
        k = (int(slot["row"]), key)
        if k in first:
            rec.node.dup_of = first[k]
        else:
            first[k] = rec.node.id
    return records


def category_reason(rec: ProductRecord) -> str:
    """상분류가 null인 이유(리포트용): non_garment · empty · noise."""
    raw = rec.node.category_raw
    if not raw:
        return "empty"
    return "non_garment" if raw in NON_GARMENT else "noise"


def snap_report(records: list[ProductRecord]) -> dict[str, Any]:
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
        # v2
        "category_null_by_reason": dict(Counter(category_reason(r) for r in records if r.category_major is None)),
        "category_null_raw": dict(Counter(r.node.category_raw or "" for r in records
                                          if r.category_major is None and category_reason(r) != "non_garment").most_common()),
        "category_major_only_raw": dict(Counter(r.node.category_raw or "" for r in records
                                                if r.category_major and not r.node.category_minor).most_common()),
        "dup_of": sum(1 for r in records if r.node.dup_of),
        "detail_padding_rows": sum(1 for r in records if any(d.value == "패딩" for d in r.details)),
        "snap_ids": len({r.node.snap_id for r in records}),
    }
