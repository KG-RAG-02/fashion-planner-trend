"""02 `hateslopacademy/otpensource_dataset` → ProductRecord.

문서: docs/schemas/graph_schema.md §2.3, docs/schemas/sources/alias_map.md
- 02는 대분류·소분류별 JSON 파일(`sorted_<대분류>/sorted_<소분류>.json`)이다.
- 브랜드·가격·상품 URL이 없다 (브랜드는 출처 기준 Musinsa).
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

from .alias_map import STANDARD, AliasMap, norm_key

REPO_ID = "hateslopacademy/otpensource_dataset"
# 카테고리와 겹치는 디테일 이름: category_minor가 정해졌으면 디테일로 쓰지 않는다 (alias_map §10)
_CATEGORY_OVERLAP = {"후드", "집업", "패딩"}
# 텍스트 키워드 검색에서 뺄 값: 한 글자 값, 다른 의미가 큰 값
_SKIP_KEYWORDS = {"플레어", "없음", "없는", "none", "null"}


def find_snapshot_dir() -> Path:
    """HF 캐시에 있는 02 스냅샷 폴더. 없으면 내려받는다."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id=REPO_ID, repo_type="dataset"))


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


class OtpenNormalizer:
    def __init__(self, alias: Optional[AliasMap] = None):
        self.alias = alias or AliasMap()
        self._text_keys = self._build_text_keys()

    def _build_text_keys(self) -> dict[str, list[tuple[str, str, bool]]]:
        """텍스트 검색용 키워드: attr -> [(key, 표준 값, approx)]. 한 글자 키는 오탐이 많아 뺀다."""
        keys: dict[str, list[tuple[str, str, bool]]] = {}
        for attr in ("pattern", "detail", "shape"):
            items = []
            for key, (value, kind) in self.alias._index[attr].items():
                if len(key) < 2 or key in {norm_key(x) for x in _SKIP_KEYWORDS}:
                    continue
                items.append((key, value, kind == "approx"))
            keys[attr] = sorted(items, key=lambda x: len(x[0]), reverse=True)
        return keys

    def normalize_row(self, row: dict[str, Any]) -> ProductRecord:
        sub = _clean(row.get("sub_category"))
        name = _clean(row.get("product_name"))
        feature = _clean(row.get("feature"))
        text = norm_key(f"{name} {feature}")

        cat = self.alias.map_otpensource_category(sub, name, row.get("big_category"))
        minor: Optional[str] = cat.category_minor
        approx_fields: list[str] = []

        # 소재 (+ 카테고리 힌트: 다운·패딩은 소재가 아니라 category_minor = 패딩)
        materials: list[AttrEdge] = []
        extra_details: list[DetailEdge] = []
        extra_patterns: list[AttrEdge] = []
        for n in self.alias.normalize_tokens("material", row.get("material")):
            if n.hint.get("category_minor") and minor is None:
                minor = n.hint["category_minor"]
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
        patterns = list(extra_patterns)
        for key, value, approx in self._text_keys["pattern"]:
            if key in text and not any(p.value == value for p in patterns):
                patterns.append(AttrEdge(value=value, raw=key, approx=approx))
        details = list(extra_details)
        for key, value, approx in self._text_keys["detail"]:
            if key in text:
                if value in _CATEGORY_OVERLAP and minor is not None:
                    continue
                if not any(d.value == value for d in details):
                    details.append(DetailEdge(type=V.DetailType.DETAIL, value=value, raw=key, approx=approx))
        shape_raw = shape = None
        for key, value, approx in self._text_keys["shape"]:
            if key in text:
                shape_raw, shape = key, value
                if approx:
                    approx_fields.append("shape")
                break

        node = ProductNode(
            id=f"otpensource:{row['_file']}:{row['_index']}",
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


def normalization_report(records: list[ProductRecord], raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """적용 결과 요약 (docs alias_map §11과 비교용)."""
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
    }
