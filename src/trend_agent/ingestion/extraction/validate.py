"""자동 추출(status "auto") 검증기 — 계약 v1.1 `docs/schemas/article_extraction.md` §5 판정 정의.

| 단계 | 판정 | 등급 |
|---|---|---|
| V1 | pydantic 구조(호출자) + 정책: `name_origin == "article"`, `aliases[]` ⊂ 제목·텍스트 블록 | 오류 |
| V2 | 모든 `quote` ⊂ 그 `block_idx` 텍스트 블록(문단·소제목), 공백을 지운 부분 문자열 | 오류 |
| V3 | Item의 모든 `*_raw`·하위 `raw` ⊂ 그 Item 근거 블록들 | 오류 |
| V4 | Item 근거 블록 ≥1, 서로 다른 블록 ≤ `max_blocks`(2), 텍스트 블록 순서 간격 ≤ `max_text_gap`(1) | 오류 |
| V5 | Pattern/Detail/Styling 혼동 키워드 · `raw_text` ⊄ 근거 블록 · Style 시즌 없음 | 경고 |
| V6 | `image_idx` ∈ 크롤러 이미지 범위 (`basis`는 pydantic Literal) | 오류 |

V2·V3 논리는 `scripts/validate_labels.py`(라벨 검증)와 같다(`squash` 동일 정의, 동등성은 테스트로 확인).
T4가 `check_quotes`·`check_raws` 순수 함수를 커밋하면 그것을 import하도록 바꾼다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ...schemas.article_extraction import ArticleExtraction, ExtractedItem

__all__ = ["squash", "ValidationReport", "validate_extraction", "text_blocks", "item_raws"]

PATTERN_AS_DETAIL = ("자수", "수놓", "아플리케", "비즈", "프릴", "리본")
DETAIL_AS_PATTERN = ("프린트", "무늬", "체크", "스트라이프", "도트", "플로럴")
STYLING_WORDS = ("두른", "두르", "매치", "레이어드", "겹쳐")


def squash(s: str) -> str:
    """공백을 모두 지운다 (validate_labels.squash와 같은 정의)."""
    return re.sub(r"\s+", "", s)


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # 단계별 오류 수 (V1~V6, 경고는 W5)

    @property
    def passed(self) -> bool:
        return not self.errors

    def err(self, stage: str, msg: str) -> None:
        self.errors.append(f"{stage} {msg}")
        self.counts[stage] = self.counts.get(stage, 0) + 1

    def warn(self, msg: str) -> None:
        self.warnings.append(f"V5 {msg}")
        self.counts["W5"] = self.counts.get("W5", 0) + 1


def text_blocks(article: dict) -> dict[int, str]:
    return {b["idx"]: b.get("text") or "" for b in article.get("blocks") or [] if b.get("type") != "image"}


def item_raws(it: ExtractedItem) -> list[tuple[str, str]]:
    pairs = [("category_raw", it.category_raw), ("fit_raw", it.fit_raw), ("shape_raw", it.shape_raw),
             ("length_body_raw", it.length_body_raw), ("length_sleeve_raw", it.length_sleeve_raw),
             ("styling_raw", it.styling_raw), ("styling_accessory", it.styling_accessory)]
    pairs += [("colors.raw", c.raw) for c in it.colors] + [("materials.raw", m.raw) for m in it.materials]
    pairs += [("patterns.raw", p.raw) for p in it.patterns] + [("details.raw", d.raw) for d in it.details]
    return [(f, r) for f, r in pairs if r]


def _short(s: str, n: int = 40) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def validate_extraction(ext: ArticleExtraction, article: dict, *, max_blocks: int = 2, max_text_gap: int = 1,
                        extra_errors: Optional[Iterable[str]] = None) -> ValidationReport:
    rep = ValidationReport()
    for e in extra_errors or []:
        stage = e.split(" ", 1)[0] if re.match(r"V\d ", e) else "V1"
        rep.errors.append(e if re.match(r"V\d ", e) else f"V1 {e}")
        rep.counts[stage] = rep.counts.get(stage, 0) + 1
    texts = text_blocks(article)
    order = [b["idx"] for b in article.get("blocks") or [] if b.get("type") != "image"]
    pos = {idx: i for i, idx in enumerate(order)}
    title_and_text = squash((article.get("title") or "") + "".join(texts.values()))
    n_images = len(article.get("images") or [])

    def check_quote(where: str, block_idx: int, quote: str) -> None:
        text = texts.get(block_idx)
        if text is None:
            rep.err("V2", f"{where}: block {block_idx}은 텍스트 블록이 아님")
        elif not squash(quote) or squash(quote) not in squash(text):
            rep.err("V2", f"{where}: quote가 block {block_idx}에 없음: \"{_short(quote)}\"")

    for s in ext.styles:
        sw = f"Style[{s.name}]"
        if s.name_origin != "article":
            rep.err("V1", f"{sw}: name_origin은 article이어야 함")
        for a in s.aliases:
            if not squash(a) or squash(a) not in title_and_text:
                rep.err("V1", f"{sw}: alias \"{_short(a)}\"가 제목·본문에 없음")
        for e in s.evidence:
            check_quote(f"{sw}.evidence", e.block_idx, e.quote)
        for d in s.description:
            for e in d.evidence:
                check_quote(f"{sw}.description", e.block_idx, e.quote)
        for f in s.focus_attributes:
            for e in f.evidence:
                check_quote(f"{sw}.focus[{f.attribute.value}]", e.block_idx, e.quote)
        if not s.seasons:
            rep.warn(f"{sw}: seasons 없음(기사에 명시된 시즌 미확인)")
        for it in s.items:
            iw = f"{sw}.Item[{it.name}]"
            if not it.evidence:
                rep.err("V4", f"{iw}: evidence 없음")
            for e in it.evidence:
                check_quote(f"{iw}.evidence", e.block_idx, e.quote)
            ev_blocks = sorted({e.block_idx for e in it.evidence})
            if len(ev_blocks) > max_blocks:
                rep.err("V4", f"{iw}: 근거 블록 {len(ev_blocks)}개 > {max_blocks} ({ev_blocks})")
            known = [b for b in ev_blocks if b in pos]
            for a, b in zip(known, known[1:]):
                if pos[b] - pos[a] > max_text_gap:
                    rep.err("V4", f"{iw}: 근거 블록 {a}·{b}가 인접하지 않음(사이 텍스트 블록 {pos[b] - pos[a] - 1}개)")
            ev_text = squash("".join(texts.get(b, "") for b in ev_blocks))
            for fname, raw in item_raws(it):
                if squash(raw) not in ev_text:
                    rep.err("V3", f"{iw}.{fname} \"{_short(raw)}\"가 근거 블록 {ev_blocks}에 없음")
            if it.raw_text and squash(it.raw_text) not in ev_text:
                rep.warn(f"{iw}: raw_text가 근거 블록의 부분 문자열이 아님")
            for p in it.patterns:
                if any(k in p.raw for k in PATTERN_AS_DETAIL):
                    rep.warn(f"{iw}: 패턴 \"{_short(p.raw)}\"는 디테일일 수 있음")
            for d in it.details:
                if any(k in d.raw for k in DETAIL_AS_PATTERN):
                    rep.warn(f"{iw}: 디테일 \"{_short(d.raw)}\"는 패턴일 수 있음")
                if any(k in d.raw for k in STYLING_WORDS):
                    rep.warn(f"{iw}: 디테일 \"{_short(d.raw)}\"는 스타일링일 수 있음")
            for link in it.image_links:
                if not (0 <= link.image_idx < n_images):
                    rep.err("V6", f"{iw}: image_idx {link.image_idx}가 크롤러 이미지 범위 밖(0..{n_images - 1})")
    return rep
