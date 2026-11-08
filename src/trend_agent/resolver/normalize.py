"""Tier2 어휘 분절 — 정규화된 용어를 별칭 사전(AliasMap) 키로 최장일치 분절한다.

정규화 자체는 계약 함수 `trend_agent.schemas.graph_schema.normalize_term()` 하나다(이 모듈은 분절만 한다).

- 토큰 우선순위(같은 길이): category > color > material > pattern > detail > neckline > collar > fit
  > 상분류어(상의·하의·아우터·원피스) > 점수에 쓰지 않는 어휘(기장·소매·실루엣) > 불용어.
- **근사(approx) 매핑은 쓰지 않는다**(팀 규칙) — 근사 토큰이 있으면 `vocab_approx_only`/`vocab_partial`로 다음 Tier에 넘긴다.
- 판정 코드: `vocab_full`(전부 덮이고 쓸 수 있는 토큰 ≥1) · `vocab_partial`(못 덮은 글자나 근사 토큰이 있음) ·
  `vocab_approx_only`(근사 토큰으로만 덮임) · `vocab_none`(쓸 수 있는 토큰 없음).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from trend_agent.ingestion.normalization.alias_map import AliasMap

SCORED_PRIORITY = ("category", "color", "material", "pattern", "detail", "neckline", "collar", "fit")
UNSCORED = ("body_length", "sleeve_length", "shape")  # 아는 단어지만 점수에 쓰지 않는다
MAJOR_WORDS = ("상의", "하의", "아우터", "원피스")
STOPWORDS = ("트렌드", "스타일", "아이템", "컬러", "패션", "코디", "룩", "색", "의")
MAX_TOKEN = 20


@dataclass(frozen=True)
class Token:
    text: str
    attr: str            # category·color·…·fit / major / body_length·sleeve_length·shape / stopword
    value: Optional[str]
    kind: str            # standard | alias | approx | route | major | unscored | stopword

    @property
    def usable(self) -> bool:
        return self.kind in ("standard", "alias", "route", "major") and (self.attr in SCORED_PRIORITY or self.attr == "major")


@dataclass
class Segmentation:
    norm: str
    tokens: list[Token] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)

    @property
    def usable(self) -> list[Token]:
        return [t for t in self.tokens if t.usable]

    @property
    def approx(self) -> list[Token]:
        return [t for t in self.tokens if t.kind == "approx"]

    @property
    def unscored(self) -> list[Token]:
        return [t for t in self.tokens if t.kind == "unscored"]

    @property
    def code(self) -> str:
        if not self.usable:
            return "vocab_approx_only" if self.approx and not self.uncovered else "vocab_none"
        if self.uncovered or self.approx:
            return "vocab_partial"
        return "vocab_full"

    def describe(self) -> str:
        parts = [f"{t.text}→{t.attr}:{t.value}" + ("(근사)" if t.kind == "approx" else "") for t in self.tokens
                 if t.kind not in ("stopword",)]
        if self.uncovered:
            parts.append("미해석 " + "·".join(self.uncovered))
        return ", ".join(parts) or "(토큰 없음)"


class Segmenter:
    def __init__(self, alias: Optional[AliasMap] = None):
        self.alias = alias or AliasMap()

    def _match(self, s: str) -> Optional[Token]:
        for attr in SCORED_PRIORITY:
            n = self.alias.normalize(attr, s)
            if n.matched_by in ("standard", "alias", "approx") and n.value is not None:
                return Token(s, attr, n.value, n.matched_by)
            if n.matched_by == "route" and n.route_to in SCORED_PRIORITY and n.value is not None:
                return Token(s, n.route_to, n.value, "route")
        if s in MAJOR_WORDS:
            return Token(s, "major", s, "major")
        for attr in UNSCORED:
            n = self.alias.normalize(attr, s)
            if n.matched_by in ("standard", "alias", "approx") and n.value is not None:
                return Token(s, attr, n.value, "unscored")
        if s in STOPWORDS:
            return Token(s, "stopword", None, "stopword")
        return None

    def segment(self, norm: str) -> Segmentation:
        """정규화된 용어(normalize_term 결과)를 왼쪽부터 최장일치로 분절한다."""
        out = Segmentation(norm=norm)
        i, miss = 0, ""
        while i < len(norm):
            tok = None
            for j in range(min(len(norm), i + MAX_TOKEN), i, -1):
                tok = self._match(norm[i:j])
                if tok is not None:
                    break
            if tok is None:
                miss += norm[i]
                i += 1
                continue
            if miss:
                out.uncovered.append(miss)
                miss = ""
            out.tokens.append(tok)
            i += len(tok.text)
        if miss:
            out.uncovered.append(miss)
        return out
