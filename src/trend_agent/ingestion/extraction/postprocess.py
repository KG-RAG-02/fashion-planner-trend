"""LLM 출력 후처리(코드) — pydantic 검증 **전** dict에 적용한다.

1. 이미지: `image_links[].image_block_idx`(프롬프트 블록 번호) → `image_idx`(크롤러 `images[].image_idx`).
   이미지 블록이 아니거나 범위 밖이면 링크를 빼고 V6 오류로 돌려준다(자동 수정하지 않고 재시도·검토로 보낸다).
2. 별칭 사전(AliasMap)으로 `*_raw`를 다시 매핑한다(문서 §3: "사전이 이기면 사전 값"):
   - 사전 정확 일치(standard·alias)가 LLM 값과 다르면 **사전 값으로 교체** + `alias_override:<필드>:<raw>:<llm>-><사전>`
   - 사전이 "값 없음"(null_values)인데 LLM이 값을 냈으면 null + `alias_null:<필드>:<raw>:<llm>`
   - 근사(approx)는 바꾸지 않는다: 같으면 `approx:<필드>:<raw>-><값>`, 다르면 `alias_disagree:<필드>:<raw>:<llm>|<사전>`
   - 속성 간 이동(route)은 표시만 `alias_route:<필드>:<raw>-><속성>:<값>`, 힌트(예: 소재 "다운" → 하분류 패딩)도
     표시만 `alias_hint:<필드>:<raw>-><키>=<값>`
   - 사전에 없는 raw는 LLM 값을 그대로 둔다.
3. `category_major`는 pydantic(`ExtractedItem._derive_major`)이 `category_minor`에서 도출하고 불일치를 표시한다.
4. (선택, 기본 꺼짐) `dedupe`: Style 안의 중복 `focus_attributes`·`seasons`를 합친다(`merge_duplicates`, P1 경고).
5. (선택, 기본 꺼짐) `name_fix`: Style 이름 끝에 LLM이 덧붙인 '트렌드'를 떼어 기사 원문 표현으로 되돌린다
   (`fix_style_names`, P2 경고). 규칙의 기준 문서는 docs/pipeline.md §6.7(D-27·D-28)이다.
"""
from __future__ import annotations

import copy
import re
from functools import lru_cache
from typing import Any, Optional

from ...schemas.vocabulary import MINOR_TO_MAJOR, CategoryMinor
from ..normalization.alias_map import AliasMap

__all__ = ["convert_image_links", "apply_alias_map", "apply_length_area", "merge_duplicates", "fix_style_names",
           "postprocess", "get_alias_map", "LENGTH_AREA_FIX", "NAME_FIX_SUFFIXES"]

# 영역별 기장 통일(선택, 기본 꺼짐): 사용자 결정 L-맥시(2026-11-08, 하의·원피스 롱 → 맥시)는 2026-11-10 폐기됐다
# (pipeline D-38: 롱은 롱). 이전 실험 재현용으로만 남긴다. 켜면 `length_area:length_body:롱->맥시` 표시를 남긴다.
LENGTH_AREA_FIX = {"하의": {"롱": "맥시"}, "원피스": {"롱": "맥시"}}

# Item 스칼라 (raw 필드, 값 필드, 사전 속성)
SCALAR_FIELDS = (
    ("category_raw", "category_minor", "category"),
    ("fit_raw", "fit", "fit"),
    ("shape_raw", "shape", "shape"),
    ("length_body_raw", "length_body", "body_length"),
    ("length_sleeve_raw", "length_sleeve", "sleeve_length"),
)
LIST_FIELDS = (("colors", "color"), ("materials", "material"), ("patterns", "pattern"))
DETAIL_ATTR = {"detail": "detail", "neckline": "neckline", "collar": "collar"}


@lru_cache(maxsize=1)
def get_alias_map() -> AliasMap:
    return AliasMap()


def _items(doc: dict):
    for si, style in enumerate(doc.get("styles") or []):
        for ii, item in enumerate(style.get("items") or []):
            yield si, ii, style, item


def convert_image_links(doc: dict, article: dict) -> list[str]:
    """제자리 변환. 반환 = V6 오류 목록."""
    blocks = {b["idx"]: b for b in article.get("blocks") or []}
    n_images = len(article.get("images") or [])
    errors: list[str] = []
    for _si, _ii, style, item in _items(doc):
        kept = []
        for link in item.get("image_links") or []:
            if "image_idx" in link and "image_block_idx" not in link:
                kept.append(link)
                continue
            bidx = link.get("image_block_idx")
            b = blocks.get(bidx)
            where = f"Style[{style.get('name')}].Item[{item.get('name')}].image_links"
            if b is None or b.get("type") != "image":
                errors.append(f"V6 {where}: block {bidx}은 이미지 블록이 아님")
                continue
            img_idx = b.get("image_idx")
            if not isinstance(img_idx, int) or not (0 <= img_idx < n_images):
                errors.append(f"V6 {where}: block {bidx}의 image_idx {img_idx}가 크롤러 이미지 범위(0..{n_images - 1}) 밖")
                continue
            kept.append({"image_idx": img_idx, "basis": link.get("basis")})
        item["image_links"] = kept
    return errors


def _flag(item: dict, text: str) -> None:
    flags = item.setdefault("review_flags", [])
    if text not in flags:
        flags.append(text)


def _remap(item: dict, field: str, raw: Optional[str], llm_value: Any, attribute: str, am: AliasMap,
           setter) -> None:
    if not raw:
        return
    n = am.normalize(attribute, raw)
    kind = n.matched_by
    if kind in ("standard", "alias"):
        if n.value != llm_value:
            setter(n.value)
            _flag(item, f"alias_override:{field}:{raw}:{llm_value}->{n.value}")
    elif kind == "null_value":
        if llm_value is not None:
            setter(None)
            _flag(item, f"alias_null:{field}:{raw}:{llm_value}")
    elif kind == "approx":
        if n.value == llm_value:
            _flag(item, f"approx:{field}:{raw}->{n.value}")
        else:
            _flag(item, f"alias_disagree:{field}:{raw}:{llm_value}|{n.value}")
    elif kind == "route":
        _flag(item, f"alias_route:{field}:{raw}->{n.route_to}:{n.value}")
    elif kind == "hint":
        hint = ",".join(f"{k}={v}" for k, v in sorted(n.hint.items()))
        _flag(item, f"alias_hint:{field}:{raw}->{hint}")


def _remap_color(item: dict, entry: dict, am: AliasMap) -> None:
    raw, llm_value = entry.get("raw"), entry.get("value")
    if not raw:
        return
    results = am.normalize_color(raw)
    if len(results) != 1:
        vals = sorted({r.value for r in results if r.value})
        if llm_value not in vals and vals:
            _flag(item, f"alias_disagree:colors:{raw}:{llm_value}|{'/'.join(vals)}")
        return
    n = results[0]
    if n.matched_by in ("standard", "alias"):
        if n.value != llm_value:
            entry["value"] = n.value
            _flag(item, f"alias_override:colors:{raw}:{llm_value}->{n.value}")
    elif n.matched_by == "null_value":
        if llm_value is not None:
            entry["value"] = None
            _flag(item, f"alias_null:colors:{raw}:{llm_value}")
    elif n.matched_by == "approx":
        if n.value == llm_value:
            _flag(item, f"approx:colors:{raw}->{n.value}")
        else:
            _flag(item, f"alias_disagree:colors:{raw}:{llm_value}|{n.value}")


def apply_alias_map(doc: dict, am: Optional[AliasMap] = None) -> None:
    am = am or get_alias_map()
    for _si, _ii, _style, item in _items(doc):
        for raw_f, val_f, attr in SCALAR_FIELDS:
            _remap(item, val_f, item.get(raw_f), item.get(val_f), attr, am,
                   lambda v, it=item, f=val_f: it.__setitem__(f, v))
        for list_f, attr in LIST_FIELDS:
            for entry in item.get(list_f) or []:
                if attr == "color":
                    _remap_color(item, entry, am)
                else:
                    _remap(item, list_f, entry.get("raw"), entry.get("value"), attr, am,
                           lambda v, e=entry: e.__setitem__("value", v))
        for entry in item.get("details") or []:
            attr = DETAIL_ATTR.get(entry.get("type") or "")
            if attr:
                _remap(item, f"details.{attr}", entry.get("raw"), entry.get("value"), attr, am,
                       lambda v, e=entry: e.__setitem__("value", v))


def _major_of(item: dict) -> Optional[str]:
    minor = item.get("category_minor")
    if minor:
        try:
            return MINOR_TO_MAJOR[CategoryMinor(minor)].value
        except ValueError:
            return None
    return item.get("category_major")


def apply_length_area(doc: dict) -> None:
    for _si, _ii, _style, item in _items(doc):
        fix = LENGTH_AREA_FIX.get(_major_of(item) or "", {})
        old = item.get("length_body")
        if old in fix:
            item["length_body"] = fix[old]
            _flag(item, f"length_area:length_body:{old}->{fix[old]}")


_LEVEL_RANK = {"core": 2, "supporting": 1}


def merge_duplicates(doc: dict) -> list[str]:
    """제자리 수정. Style 안의 중복을 하나로 합치고 무엇을 합쳤는지(P1 경고 문구)를 돌려준다. 모델과 무관한 코드 규칙.

    - `focus_attributes`: 같은 `attribute`가 여럿이면 처음 나온 자리에 하나만 남긴다. level은 core가 supporting보다 우선
      (pipeline §7.2 병합 규칙과 같다), evidence는 (block_idx, quote) 기준 합집합(순서 보존).
    - `seasons`: 같은 {year, season}이 여럿이면 하나만 남긴다.
    core가 3개를 넘는 경우는 어느 것을 낮출지 근거가 없어 고치지 않는다(V1 오류로 남는다).
    """
    notes: list[str] = []
    for style in doc.get("styles") or []:
        name = style.get("name")
        merged: dict[Any, dict] = {}
        order: list[Any] = []
        for f in style.get("focus_attributes") or []:
            key = f.get("attribute")
            if key not in merged:
                merged[key] = {**f, "evidence": list(f.get("evidence") or [])}
                order.append(key)
                continue
            m = merged[key]
            if _LEVEL_RANK.get(f.get("level"), 0) > _LEVEL_RANK.get(m.get("level"), 0):
                m["level"] = f.get("level")
            seen = {(e.get("block_idx"), e.get("quote")) for e in m["evidence"]}
            m["evidence"] += [e for e in f.get("evidence") or [] if (e.get("block_idx"), e.get("quote")) not in seen]
        before = len(style.get("focus_attributes") or [])
        if before != len(order):
            style["focus_attributes"] = [merged[k] for k in order]
            notes.append(f"P1 Style[{name}]: focus_attributes 중복 {before}개 → {len(order)}개로 합침 "
                         f"({', '.join(f'{k}={merged[k].get('level')}' for k in order)})")
        seasons, keys = [], set()
        for s in style.get("seasons") or []:
            k = (s.get("year"), s.get("season"))
            if k not in keys:
                keys.add(k)
                seasons.append(s)
        if len(seasons) != len(style.get("seasons") or []):
            notes.append(f"P1 Style[{name}]: seasons 중복 {len(style['seasons'])}개 → {len(seasons)}개로 합침")
            style["seasons"] = seasons
    return notes


# P2 이름 후처리 (pipeline.md §6.7, D-28): 떼는 일반어 허용 목록. 늘리려면 사용자 승인이 필요하다.
NAME_FIX_SUFFIXES = ("트렌드", "trend")
_NAME_TRAIL = " \t -–—·,.:;!?'\"‘’“”"


def _sq(s: str) -> str:
    """공백을 지우고 소문자로 (영문 대소문자 무시 비교용)."""
    return re.sub(r"\s+", "", s or "").lower()


def _article_text(article: dict) -> str:
    """제목 + 텍스트 블록(문단·소제목)을 비교용으로 이은 것 (검증기 N3와 같은 범위)."""
    texts = [b.get("text") or "" for b in article.get("blocks") or [] if b.get("type") != "image"]
    return _sq((article.get("title") or "") + "".join(texts))


def fix_style_names(doc: dict, article: dict) -> list[str]:
    """제자리 수정. 아래 ①~⑤를 모두 만족할 때만 Style 이름 끝의 일반어를 한 번 뗀다. 기록(P2·N3 문구)을 돌려준다.

    ① 이름 전체가 기사에 없다 ② 허용 목록(`NAME_FIX_SUFFIXES`)의 일반어로 끝난다 ③ 뗀 나머지가 2글자 이상이다
    ④ 뗀 나머지가 기사에 있다 ⑤ 뗀 이름이 같은 기사의 다른 Style 이름과 겹치지 않는다 (⑤에 걸리면 N3 문구만 남긴다)
    `definition`·`description`·`evidence`·`aliases`는 건드리지 않는다.
    """
    text = _article_text(article)
    styles = doc.get("styles") or []
    names = [s.get("name") or "" for s in styles]
    notes: list[str] = []
    for i, style in enumerate(styles):
        name = style.get("name") or ""
        if not _sq(name) or _sq(name) in text:                                   # ①
            continue
        core = name.rstrip(_NAME_TRAIL)
        suf = next((s for s in NAME_FIX_SUFFIXES if core.lower().endswith(s.lower())), None)
        if suf is None:                                                           # ②
            continue
        new = core[: len(core) - len(suf)].rstrip(_NAME_TRAIL)
        if len(_sq(new)) < 2 or _sq(new) not in text:                             # ③ ④
            continue
        if _sq(new) in {_sq(n) for j, n in enumerate(names) if j != i}:           # ⑤
            notes.append(f"N3 Style[{name}]: 이름 끝 '{suf}'를 떼면 같은 기사의 다른 Style 이름과 같아져 고치지 않음")
            continue
        style["name"] = new
        names[i] = new
        notes.append(f"P2 Style[{name}]: 이름 끝 '{suf}' 제거 → '{new}' (기사에 '{name}' 없음, '{new}' 있음)")
    return notes


def postprocess(raw_doc: dict, article: dict, *, am: Optional[AliasMap] = None, length_area: bool = False,
                dedupe: bool = False, name_fix: bool = False,
                notes: Optional[list[str]] = None) -> tuple[dict, list[str]]:
    """(pydantic 입력 dict, V6 오류). 원본 dict는 바꾸지 않는다.

    선택 후처리(라이브러리 기본값은 꺼짐. P1·P2는 채택돼 실행 스크립트가 기본으로 켠다 — pipeline.md D-27, §6.7): `dedupe=True`면 P1 `merge_duplicates`,
    `name_fix=True`면 P2 `fix_style_names`를 이 순서로 적용하고 기록을 `notes`(주면)에 덧붙인다.
    """
    doc = copy.deepcopy(raw_doc)
    doc["article_id"] = str(article["article_id"])
    errors = convert_image_links(doc, article)
    apply_alias_map(doc, am)
    if length_area:
        apply_length_area(doc)
    found: list[str] = []
    if dedupe:
        found += merge_duplicates(doc)
    if name_fix:
        found += fix_style_names(doc, article)
    if notes is not None:
        notes.extend(found)
    return doc, errors
