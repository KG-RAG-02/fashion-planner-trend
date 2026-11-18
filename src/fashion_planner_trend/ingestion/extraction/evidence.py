"""근거 판정 V2·V3. 자동 추출 검증(`validate.py`)과 정답 라벨 검증(`scripts/validate_labels.py`)이 같은 함수를 쓴다.

- V2 근거 존재: 모든 quote가 해당 block_idx의 텍스트 블록(문단·소제목)에 있는가. 공백을 지운 부분 문자열로 본다.
- V3 속성 존재: Item의 모든 `*_raw`·하위 객체 `raw`가 그 Item의 근거 블록 안에 있는가.
"""
from __future__ import annotations

import re

from ...schemas.article_extraction import ArticleExtraction, ExtractedItem

__all__ = ["squash", "text_blocks", "raw_values", "quote_errors", "check_quotes", "check_raws"]


def squash(s: str) -> str:
    """공백을 모두 지운다."""
    return re.sub(r"\s+", "", s)


def text_blocks(article: dict) -> dict[int, str]:
    """기사 → {block idx: text}. 이미지 블록은 빼고 문단·소제목만 남긴다(V2·V3 검사의 입력)."""
    return {b["idx"]: b.get("text") or "" for b in article.get("blocks") or [] if b.get("type") != "image"}


def raw_values(it: ExtractedItem) -> list[str | None]:
    """Item의 원문 표현 필드(*_raw, 하위 객체 raw) — V3 검사 대상. 빈 값도 그대로 둔다."""
    raws = [it.category_raw, it.fit_raw, it.shape_raw, it.length_body_raw, it.length_sleeve_raw, it.styling_raw,
            it.styling_accessory]
    return raws + [c.raw for c in it.colors] + [m.raw for m in it.materials] + [p.raw for p in it.patterns] + \
        [d.raw for d in it.details]


def quote_errors(ext: ArticleExtraction, text_by_idx: dict[int, str], require_item_evidence: bool) -> list[str]:
    """V2 오류 문구 목록. `require_item_evidence`면 근거가 없는 Item도 오류로 센다(라벨 검증 규칙)."""
    errors: list[str] = []

    def check_quote(where: str, block_idx: int, quote: str) -> None:
        text = text_by_idx.get(block_idx)
        if text is None:
            errors.append(f"{where}: block_idx {block_idx}에 문단이 없음")
        elif squash(quote) not in squash(text):
            errors.append(f"{where}: quote가 block {block_idx}에 없음: {quote[:40]}")

    for s in ext.styles:
        for e in s.evidence:
            check_quote(f"Style[{s.name}].evidence", e.block_idx, e.quote)
        for d in s.description:
            for e in d.evidence:
                check_quote(f"Style[{s.name}].description", e.block_idx, e.quote)
        for f in s.focus_attributes:
            for e in f.evidence:
                check_quote(f"Style[{s.name}].focus[{f.attribute.value}]", e.block_idx, e.quote)
        for it in s.items:
            if require_item_evidence and not it.evidence:
                errors.append(f"Item[{it.name}]: evidence 없음")
            for e in it.evidence:
                check_quote(f"Item[{it.name}].evidence", e.block_idx, e.quote)
    return errors


def check_quotes(ext: ArticleExtraction, text_by_idx: dict[int, str]) -> list[str]:
    """V2 근거 존재 오류 문구 목록 (근거가 없는 Item은 여기서 세지 않는다)."""
    return quote_errors(ext, text_by_idx, require_item_evidence=False)


def check_raws(ext: ArticleExtraction, text_by_idx: dict[int, str]) -> list[str]:
    """V3 속성 존재 문구 목록. 라벨 검증에서는 경고, 자동 추출 검증에서는 오류로 쓴다."""
    warnings: list[str] = []
    for s in ext.styles:
        for it in s.items:
            ev_text = squash("".join(text_by_idx.get(e.block_idx, "") for e in it.evidence))
            for r in raw_values(it):
                if r and squash(r) not in ev_text:
                    warnings.append(f"Item[{it.name}]: raw '{r}'가 근거 문단에 없음 (지시 표현이면 notes에 적을 것)")
    return warnings
