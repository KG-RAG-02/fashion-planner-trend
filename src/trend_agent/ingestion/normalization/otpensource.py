"""02 `hateslopacademy/otpensource_dataset` → ProductRecord.

문서: docs/schemas/graph_schema.md §2.3, docs/schemas/sources/alias_map.md
- 02는 대분류·소분류별 JSON 파일(`sorted_<대분류>/sorted_<소분류>.json`)이다.
- 브랜드·가격·상품 URL이 없다 (브랜드는 출처 기준 Musinsa).

v2 (T2): (e) 패딩 힌트는 상분류가 비었거나 아우터일 때만 · (f) 상품명·feature를 따로 토큰 단위로 찾는다(text_match) ·
(h) 빈 행은 상분류 없이 남겨 적재에서 빠지고(L0 결정), 원본 필드가 모두 같은 뒤쪽 행은 `dup_of`로 표시만 한다.
세트 분해는 set_split.py.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import AttrEdge, DetailEdge, ProductNode, ProductRecord

from .alias_map import AliasMap, OtpenCategory, norm_key
from .text_match import TextMatcher

REPO_ID = "hateslopacademy/otpensource_dataset"
REVISION = "0e2cb23abedd934fdb10735c8450063a785ff93c"  # notebook 02 출력 기준 고정 리비전(JSON 45개, 8,827행)
RAW_FIELDS = ("big_category", "sub_category", "gender", "season", "product_name", "color", "material", "feature", "image_url")
# 카테고리와 겹치는 디테일 이름: category_minor가 정해졌으면 디테일로 쓰지 않는다 (alias_map §10)
_CATEGORY_OVERLAP = {"후드", "집업", "패딩"}
# 텍스트 키워드 검색에서 뺄 값: 한 글자 값, 다른 의미가 큰 값
_SKIP_KEYWORDS = {"플레어", "없음", "없는", "none", "null"}


def find_snapshot_dir(revision: str = REVISION) -> Path:
    """HF 캐시에 있는 02 스냅샷 폴더(고정 리비전). 없으면 내려받는다(HF_HOME을 따른다)."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id=REPO_ID, repo_type="dataset", revision=revision, allow_patterns=["*.json"]))


def iter_rows(snapshot_dir: Path | str) -> Iterable[dict[str, Any]]:
    """파일 순서대로 행을 낸다. 행마다 `_file`, `_index`를 붙인다."""
    for path in sorted(Path(snapshot_dir).rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data = data.get("data") or data.get("items") or list(data.values())
        for i, row in enumerate(data):
            if isinstance(row, dict):
                yield {**row, "_file": f"{path.parent.name}/{path.name}", "_index": i}


def _clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _gender(v: Any) -> Optional[V.Gender]:
    s = _clean(v)
    if s in ("남", "여", "공용"):
        return V.Gender(s)
    if s == "성별무관":
        return V.Gender.UNISEX
    return None


def _season(v: Any) -> Optional[V.Season]:
    s = _clean(v)
    return V.Season(s) if s in ("SS", "FW") else None


def product_id(row: dict[str, Any]) -> str:
    return f"otpensource:{row['_file']}:{row['_index']}"


def is_empty_row(row: dict[str, Any]) -> bool:
    """(h) 상품명·이미지 URL·소분류가 모두 빈 행(02 원천 65행). 대분류만 있어도 상품 정보가 없다."""
    return not any(_clean(row.get(k)) for k in ("product_name", "image_url", "sub_category"))


def raw_signature(row: dict[str, Any]) -> str:
    return json.dumps({k: _clean(row.get(k)) for k in RAW_FIELDS}, ensure_ascii=False, sort_keys=True)


def mark_exact_duplicates(records: list[ProductRecord], rows: list[dict[str, Any]]) -> list[ProductRecord]:
    """(h) 원본 필드(RAW_FIELDS)가 모두 같은 뒤쪽 행에 `dup_of` = 앞 행 id. 빈 행은 표시하지 않는다(L0 결정: 지우지 않음)."""
    first: dict[str, str] = {}
    for rec, row in zip(records, rows):
        if is_empty_row(row):
            continue
        sig = raw_signature(row)
        if sig in first:
            rec.node.dup_of = first[sig]
        else:
            first[sig] = rec.node.id
    return records


class OtpenNormalizer:
    def __init__(self, alias: Optional[AliasMap] = None):
        self.alias = alias or AliasMap()
        self.matchers = self._build_matchers()

    def _build_matchers(self) -> dict[str, TextMatcher]:
        """텍스트 검색용 키워드: attr -> TextMatcher. 한 글자 키는 오탐이 많아 뺀다."""
        guards = (self.alias._cfg.get("text_guard") or {}).get("guards") or {}
        skip = {norm_key(x) for x in _SKIP_KEYWORDS}
        out: dict[str, TextMatcher] = {}
        for attr in ("pattern", "detail", "shape"):
            items = [(key, value, kind == "approx") for key, (value, kind) in self.alias._index[attr].items()
                     if len(key) >= 2 and key not in skip]
            out[attr] = TextMatcher(items, guards)
        return out

    def find_text(self, attr: str, *texts: Optional[str]) -> list[tuple[str, str, bool]]:
        """상품명·feature를 **따로** 찾고 합친다(앞 텍스트 우선, 값마다 한 번)."""
        out: list[tuple[str, str, bool]] = []
        seen: set[str] = set()
        for text in texts:
            for value, raw, approx in self.matchers[attr].find(text):
                if value not in seen:
                    out.append((value, raw, approx))
                    seen.add(value)
        return out

    def normalize_row(self, row: dict[str, Any]) -> ProductRecord:
        sub = _clean(row.get("sub_category"))
        name = _clean(row.get("product_name"))
        feature = _clean(row.get("feature"))

        # (h) 빈 행은 대분류가 있어도 상분류를 주지 않는다 → 그래프 적재 제외(행은 지우지 않는다, L0 결정)
        cat = OtpenCategory(raw=sub) if is_empty_row(row) else self.alias.map_otpensource_category(sub, name, row.get("big_category"))
        minor: Optional[str] = cat.category_minor
        approx_fields: list[str] = []

        # 소재 (+ 카테고리 힌트: 다운·패딩은 소재가 아니라 category_minor = 패딩 — 상분류가 비었거나 아우터일 때만.
        #  상의·하의·원피스면 카테고리는 그대로 두고 Detail 패딩. v1 minor=패딩·major=하의 10건은 전부 패딩 팬츠였다 (e))
        materials: list[AttrEdge] = []
        extra_details: list[DetailEdge] = []
        extra_patterns: list[AttrEdge] = []
        for n in self.alias.normalize_tokens("material", row.get("material")):
            if n.hint.get("category_minor"):
                if minor is None and cat.category_major in (None, V.CategoryMajor.OUTER.value):
                    minor = n.hint["category_minor"]
                elif minor is None and not any(d.value == V.DetailName.PADDING.value for d in extra_details):
                    extra_details.append(DetailEdge(type=V.DetailType.DETAIL, value=V.DetailName.PADDING.value, raw=n.raw))
            elif n.route_to == "detail" and n.value:
                extra_details.append(DetailEdge(type=V.DetailType.DETAIL, value=n.value, raw=n.raw))
            elif n.value and n.route_to is None:
                materials.append(AttrEdge(value=n.value, raw=n.raw, approx=n.approx))

        # 색
        colors: list[AttrEdge] = []
        for n in self.alias.normalize_color(row.get("color")):
            if n.value:
                colors.append(AttrEdge(value=n.value, raw=n.raw, approx=n.approx))

        # 길이 (소분류 이름에서)
        length_body = length_sleeve = None
        if cat.length_body_raw:
            n = self.alias.normalize("body_length", cat.length_body_raw)
            length_body = n.value
            if n.approx:
                approx_fields.append("length_body")
        if cat.length_sleeve_raw:
            n = self.alias.normalize("sleeve_length", cat.length_sleeve_raw)
            length_sleeve = n.value
            if n.approx:
                approx_fields.append("length_sleeve")

        # 패턴·디테일·세이프: 상품명·feature 키워드 (없으면 미확인)
        patterns, details, shape_raw, shape, shape_approx = self.text_attributes(
            [name, feature], minor, extra_patterns, extra_details)
        if shape_approx:
            approx_fields.append("shape")

        node = ProductNode(
            id=product_id(row),
            name=name or None,
            image_url=_clean(row.get("image_url")) or None,
            source="otpensource",
            kind="catalog_item",
            gender=_gender(row.get("gender")),
            season=_season(row.get("season")),
            category_raw=sub or None,
            category_minor=minor,
            length_body_raw=cat.length_body_raw,
            length_body=length_body,
            length_sleeve_raw=cat.length_sleeve_raw,
            length_sleeve=length_sleeve,
            shape_raw=shape_raw,
            shape=shape,
            source_row=f"{row['_file']}#{row['_index']}",
            approx=approx_fields,
        )
        return ProductRecord(
            node=node,
            category_major=cat.category_major,
            colors=colors,
            materials=materials,
            patterns=patterns,
            details=details,
        )

    def material_details(self, row: dict[str, Any]) -> list[DetailEdge]:
        """소재 필드에서 Detail로 옮긴 값(퀼팅 등). 세트 구성품이 부모와 공유한다."""
        return [DetailEdge(type=V.DetailType.DETAIL, value=n.value, raw=n.raw)
                for n in self.alias.normalize_tokens("material", row.get("material"))
                if n.route_to == "detail" and n.value]

    def text_attributes(
        self,
        texts: list[Optional[str]],
        minor: Optional[str],
        patterns: Optional[list[AttrEdge]] = None,
        details: Optional[list[DetailEdge]] = None,
    ) -> tuple[list[AttrEdge], list[DetailEdge], Optional[str], Optional[str], bool]:
        """텍스트에서 Pattern·Detail·Shape. 카테고리와 겹치는 디테일(후드·집업·패딩)은 하분류가 있으면 쓰지 않는다."""
        patterns = list(patterns or [])
        details = list(details or [])
        for value, raw, approx in self.find_text("pattern", *texts):
            if not any(p.value == value for p in patterns):
                patterns.append(AttrEdge(value=value, raw=raw, approx=approx))
        for value, raw, approx in self.find_text("detail", *texts):
            if value in _CATEGORY_OVERLAP and minor is not None:
                continue
            if not any(d.value == value for d in details):
                details.append(DetailEdge(type=V.DetailType.DETAIL, value=value, raw=raw, approx=approx))
        found = self.find_text("shape", *texts)
        if found:
            value, raw, approx = found[0]
            return patterns, details, raw, value, approx
        return patterns, details, None, None, False


def normalization_report(records: list[ProductRecord], raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """적용 결과 요약 (docs alias_map §11과 비교용). records는 본문(분해 후), raw_rows는 원천 행 전체."""
    n = len(records)
    has_color_raw = sum(1 for r in raw_rows if _clean(r.get("color")))
    has_mat_raw = sum(1 for r in raw_rows if _clean(r.get("material")))
    return {
        "rows": n,
        "category_minor": sum(1 for r in records if r.node.category_minor),
        "category_major_only": sum(1 for r in records if r.category_major and not r.node.category_minor),
        "category_none": sum(1 for r in records if r.category_major is None),
        "color_raw_nonempty": has_color_raw,
        "color_mapped_rows": sum(1 for r in records if r.colors),
        "color_approx_rows": sum(1 for r in records if any(c.approx for c in r.colors)),
        "material_raw_nonempty": has_mat_raw,
        "material_mapped_rows": sum(1 for r in records if r.materials),
        "material_approx_rows": sum(1 for r in records if any(m.approx for m in r.materials)),
        "pattern_rows": sum(1 for r in records if r.patterns),
        "detail_rows": sum(1 for r in records if r.details),
        "shape_rows": sum(1 for r in records if r.node.shape),
        "length_body_rows": sum(1 for r in records if r.node.length_body),
        "gender_rows": sum(1 for r in records if r.node.gender),
        "season_rows": sum(1 for r in records if r.node.season),
        "by_category_major": dict(Counter(r.category_major.value for r in records if r.category_major)),
        # v2
        "raw_rows": len(raw_rows),
        "empty_rows": sum(1 for r in raw_rows if is_empty_row(r)),
        "dup_of": sum(1 for r in records if r.node.dup_of),
        "split_components": sum(1 for r in records if r.node.parent_id),
        "split_parents": len({r.node.parent_id for r in records if r.node.parent_id}),
        "multipack": sum(1 for r in records if r.node.multipack),
        "split_candidate": sum(1 for r in records if r.node.split_candidate),
        "detail_padding_rows": sum(1 for r in records if any(d.value == "패딩" for d in r.details)),
        "image_status": dict(Counter(str(r.node.image_status) for r in records)),
    }
