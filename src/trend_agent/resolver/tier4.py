"""Tier4 — 그래프·어휘·의미 검색이 모두 실패한 용어를 LLM이 **등급으로만** 설명하고, 수치화는 코드가 한다.

- 호출: T1 `trend_agent.llm.call(prompt, schema, purpose="tier4_profile", prompt_version=PROMPT_VERSION)` (기본 구독 Codex).
- 출력 스키마: 폐쇄 어휘 enum만, 숫자 필드 없음(strict: 전부 required + nullable, additionalProperties false).
  속성 종류별 `focus` = core|supporting|null(core ≤ 3), Item별 `level` = core|supporting, 값별 `value_level` = primary|secondary,
  Item `category_major` 필수, `evidence` = 제공한 블록의 ref + 그 블록에 실제로 있는 문장, 근거가 없으면 `no_evidence = true`.
- 코드 검증(`check_profile`): enum·core ≤ 3·items 1~6·하분류↔상분류·ref ∈ 제공 블록·quote ⊂ 블록(공백 제거 비교).
  실패하면 오류 목록을 붙여 1회 재시도 → 그래도 실패면 `llm_invalid_output`. `recognized=false`는 `llm_unrecognized`.
- 수치화(`to_virtual`): 속성 배수 = weights.yaml `focus_multipliers`(Style focus와 같은 경로), 값 가중치 =
  `tier4.value_weights`, Item 비중 = `tier4.item_focus`. **LLM이 낸 숫자는 없다.**
- 캐시: `term_map.jsonl` profile 행, 키 (norm, backend, model, prompt_version, context_sha).
- 맥락: T1 `term_mentions.jsonl`의 근거 블록(최대 8개, 인용은 크롤 JSONL에서 읽는다). 없으면 "근거 없음".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import normalize_term

from .profile import VirtualItemSpec, VirtualProfile

PROMPT_VERSION = "tier4.v1"
PURPOSE = "tier4_profile"
MAX_BLOCKS = 8
MAX_ITEMS = 6
MAX_CORE = 3
FOCUS_ATTRS = ("color", "material", "pattern", "detail", "fit", "category")
DETAIL_IDS = [V.detail_node_id(t, m.value) for t, enum in V.DETAIL_ENUMS.items() for m in enum]

Level = Literal["core", "supporting"]
ValueLevel = Literal["primary", "secondary"]


# ---------- 출력 모델 (pydantic 재검증) ----------
class ColorRef(BaseModel):
    value: V.Color
    value_level: ValueLevel


class MaterialRef(BaseModel):
    value: V.Material
    value_level: ValueLevel


class PatternRef(BaseModel):
    value: V.Pattern
    value_level: ValueLevel


class DetailRef(BaseModel):
    value: Literal[tuple(DETAIL_IDS)]  # type: ignore[valid-type]
    value_level: ValueLevel


class Tier4Item(BaseModel):
    category_major: V.CategoryMajor
    category_minor: Optional[V.CategoryMinor] = None
    level: Level
    colors: list[ColorRef] = Field(default_factory=list)
    materials: list[MaterialRef] = Field(default_factory=list)
    patterns: list[PatternRef] = Field(default_factory=list)
    details: list[DetailRef] = Field(default_factory=list)
    fit: Optional[V.Fit] = None


class Tier4Focus(BaseModel):
    color: Optional[Level] = None
    material: Optional[Level] = None
    pattern: Optional[Level] = None
    detail: Optional[Level] = None
    fit: Optional[Level] = None
    category: Optional[Level] = None


class Tier4Evidence(BaseModel):
    ref: str
    quote: str


class Tier4Profile(BaseModel):
    term: str
    recognized: bool
    summary: Optional[str] = None
    focus: Tier4Focus = Field(default_factory=Tier4Focus)
    items: list[Tier4Item] = Field(default_factory=list)
    evidence: list[Tier4Evidence] = Field(default_factory=list)
    no_evidence: bool = True


# ---------- strict JSON 스키마 (손으로 고정 — 숫자 필드 없음) ----------
def _enum(values: list[str], nullable: bool = False) -> dict[str, Any]:
    if nullable:
        return {"type": ["string", "null"], "enum": [*values, None]}
    return {"type": "string", "enum": list(values)}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _refs(values: list[str]) -> dict[str, Any]:
    return {"type": "array", "items": _obj({"value": _enum(values), "value_level": _enum(["primary", "secondary"])})}


def tier4_schema() -> dict[str, Any]:
    levels = ["core", "supporting"]
    item = _obj({
        "category_major": _enum([m.value for m in V.CategoryMajor]),
        "category_minor": _enum([m.value for m in V.CategoryMinor], nullable=True),
        "level": _enum(levels),
        "colors": _refs([m.value for m in V.Color]),
        "materials": _refs([m.value for m in V.Material]),
        "patterns": _refs([m.value for m in V.Pattern]),
        "details": _refs(DETAIL_IDS),
        "fit": _enum([m.value for m in V.Fit], nullable=True),
    })
    return _obj({
        "term": {"type": "string"},
        "recognized": {"type": "boolean"},
        "summary": {"type": ["string", "null"]},
        "focus": _obj({a: _enum(levels, nullable=True) for a in FOCUS_ATTRS}),
        "items": {"type": "array", "items": item},
        "evidence": {"type": "array", "items": _obj({"ref": {"type": "string"}, "quote": {"type": "string"}})},
        "no_evidence": {"type": "boolean"},
    })


# ---------- 맥락 (T1 term_mentions.jsonl + 크롤 블록) ----------
@dataclass
class ContextBlock:
    ref: str          # "<article_id>:<block_idx>"
    article_id: str
    block_idx: int
    text: str
    role: str         # style | mention
    title: Optional[str] = None
    published: Optional[str] = None


def default_mentions_path() -> Path:
    """env `TERM_MENTIONS_PATH` → `$SHARED/extractions/term_mentions.jsonl`(T1 §4.7) → 저장소 data/processed/extractions/."""
    if os.environ.get("TERM_MENTIONS_PATH"):
        return Path(os.environ["TERM_MENTIONS_PATH"])
    if os.environ.get("SHARED"):
        return Path(os.environ["SHARED"]) / "extractions" / "term_mentions.jsonl"
    return Path(__file__).resolve().parents[3] / "data" / "processed" / "extractions" / "term_mentions.jsonl"


def read_mentions(path: Optional[Path | str]) -> dict[str, dict[str, Any]]:
    """term_mentions.jsonl → {norm: 행}. 없으면 빈 dict. norm은 계약 normalize_term()으로 다시 계산해 맞춘다."""
    if not path or not Path(path).exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                out.setdefault(normalize_term(row.get("term") or row.get("norm") or ""), row)
    return out


def context_blocks(norm: str, mentions: dict[str, dict[str, Any]], crawl: dict[str, dict[str, Any]],
                   max_blocks: int = MAX_BLOCKS) -> list[ContextBlock]:
    """그 용어가 나온 기사 블록(최대 max_blocks, 파일 순서). 제목(block_idx null)·크롤에 없는 블록은 넣지 않는다."""
    row = mentions.get(norm)
    if not row:
        return []
    out: list[ContextBlock] = []
    seen: set[str] = set()
    for ev in row.get("evidence") or []:
        aid, b = str(ev.get("article_id")), ev.get("block_idx")
        if b is None:
            continue
        art = crawl.get(aid) or {}
        blocks = {blk.get("idx"): blk for blk in art.get("blocks") or []}
        text = (blocks.get(b) or {}).get("text")
        ref = f"{aid}:{b}"
        if not text or ref in seen:
            continue
        seen.add(ref)
        out.append(ContextBlock(ref=ref, article_id=aid, block_idx=int(b), text=text, role=ev.get("role") or "mention",
                                title=art.get("title"), published=ev.get("published")))
        if len(out) >= max_blocks:
            break
    return out


def context_sha(blocks: list[ContextBlock]) -> str:
    payload = json.dumps([{"ref": b.ref, "text": b.text, "role": b.role} for b in blocks], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------- 프롬프트 ----------
def render_prompt(term: str, blocks: list[ContextBlock], errors: Optional[list[str]] = None) -> str:
    lines = [
        "[역할] 너는 패션 MD 보조다. 아래 '용어'가 가리키는 옷차림을 무신사 상품 속성으로만 설명한다.",
        "[규칙]",
        "1) 숫자·점수·비율을 쓰지 않는다. 등급만 고른다(focus·level: core/supporting, value_level: primary/secondary).",
        "2) 값은 출력 스키마의 enum 목록에서만 고른다. 목록에 없으면 비운다(지어내지 않는다).",
        "3) 이 용어를 패션 트렌드·스타일로 모르면 recognized=false로 두고 summary=null, items·evidence는 빈 배열로 둔다.",
        f"4) focus의 core는 최대 {MAX_CORE}개. 그 옷차림을 알아보게 하는 속성 종류만 core로 둔다.",
        "5) evidence는 아래 [근거 블록]의 ref와 그 블록에 실제로 있는 문장만 그대로 인용한다. 블록이 없거나 쓸 근거가 없으면 "
        "evidence=[]이고 no_evidence=true다(이때는 일반적인 패션 지식으로 판단한다).",
        f"6) items는 이 용어를 대표하는 옷 1~{MAX_ITEMS}개(상의·하의·아우터·원피스만). 신발·가방·액세서리는 넣지 않는다. "
        "category_minor를 고르면 그 하분류에 맞는 category_major를 고른다.",
        "7) 근거 블록은 그 용어가 언급된 문단이다. '끝났다·지났다'처럼 유행 종료를 말하는 언급일 수도 있으니 문맥대로 판단한다.",
        f"[용어] {term}",
        "[근거 블록]",
    ]
    if blocks:
        for b in blocks:
            head = f"{b.ref} (기사 제목: {b.title})" if b.title else b.ref
            lines.append(f"- {head}: {b.text}")
    else:
        lines.append("없음")
    if errors:
        lines.append("[이전 출력의 검증 오류 — 고쳐서 다시 출력]")
        lines += [f"- {e}" for e in errors]
    lines.append("[도구 사용 금지. 스키마에 맞는 JSON만 출력한다.]")
    return "\n".join(lines)


# ---------- 검증 ----------
def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def check_profile(data: dict[str, Any], blocks: list[ContextBlock]) -> tuple[Optional[Tier4Profile], list[str]]:
    """pydantic 재검증 + 코드 규칙. 반환 (프로필 또는 None, 오류 목록)."""
    try:
        p = Tier4Profile.model_validate(data)
    except ValidationError as e:
        return None, [f"스키마: {x['loc']}: {x['msg']}" for x in e.errors()[:10]]
    errs: list[str] = []
    if not p.recognized:
        return p, errs
    cores = [a for a in FOCUS_ATTRS if getattr(p.focus, a) == "core"]
    if len(cores) > MAX_CORE:
        errs.append(f"focus core가 {len(cores)}개다(최대 {MAX_CORE}): {cores}")
    if not 1 <= len(p.items) <= MAX_ITEMS:
        errs.append(f"items는 1~{MAX_ITEMS}개여야 한다(지금 {len(p.items)}개)")
    for k, it in enumerate(p.items):
        if it.category_minor is not None and V.MINOR_TO_MAJOR[it.category_minor] != it.category_major:
            errs.append(f"items[{k}] 하분류 {it.category_minor.value}의 상분류는 {V.MINOR_TO_MAJOR[it.category_minor].value}다")
        for key in ("colors", "materials", "patterns", "details"):
            vals = [r.value.value if hasattr(r.value, "value") else r.value for r in getattr(it, key)]
            if len(vals) != len(set(vals)):
                errs.append(f"items[{k}].{key}에 같은 값이 두 번 있다")
    by_ref = {b.ref: b for b in blocks}
    for e in p.evidence:
        b = by_ref.get(e.ref)
        if b is None:
            errs.append(f"evidence ref {e.ref!r}는 제공한 블록이 아니다")
        elif not _squash(e.quote) or _squash(e.quote) not in _squash(b.text):
            errs.append(f"evidence {e.ref}의 quote가 블록 원문에 없다")
    if not blocks and p.evidence:
        errs.append("근거 블록이 없는데 evidence가 있다")
    if p.evidence and p.no_evidence:
        errs.append("evidence가 있으면 no_evidence=false여야 한다")
    if not p.evidence and not p.no_evidence:
        errs.append("evidence가 없으면 no_evidence=true여야 한다")
    return p, errs


# ---------- 수치화 (코드) ----------
def to_virtual(p: Tier4Profile, term: str, norm: str, blocks: list[ContextBlock]) -> VirtualProfile:
    """등급 → 가상 프로필. 수치는 profile.VirtualItemSpec.value_weights / item_focus가 weights.yaml로 만든다."""
    focus_core = [a for a in FOCUS_ATTRS if getattr(p.focus, a) == "core"]
    focus_sup = [a for a in FOCUS_ATTRS if getattr(p.focus, a) == "supporting"]
    specs = []
    for it in p.items:
        specs.append(VirtualItemSpec(
            category_major=it.category_major.value,
            category_minor=it.category_minor.value if it.category_minor else None,
            level=it.level,
            colors=[(r.value.value, r.value_level) for r in it.colors],
            materials=[(r.value.value, r.value_level) for r in it.materials],
            patterns=[(r.value.value, r.value_level) for r in it.patterns],
            details=[(r.value, r.value_level) for r in it.details],
            fit=it.fit.value if it.fit else None))
    by_ref = {b.ref: b for b in blocks}
    evidence = []
    for e in p.evidence:
        b = by_ref[e.ref]
        evidence.append((b.article_id, b.block_idx, e.quote))
    return VirtualProfile(tier=4, term=term, norm=norm, definition=p.summary or term, focus_core=focus_core,
                          focus_supporting=focus_sup, items=specs, evidence=evidence, value_weighted=True)


@dataclass
class Tier4Outcome:
    code: str                                  # llm_profile | llm_cache_hit | llm_unrecognized | llm_invalid_output | llm_unavailable
    profile: Optional[Tier4Profile] = None
    raw: Optional[dict[str, Any]] = None
    backend: Optional[str] = None
    model: Optional[str] = None
    context_sha: str = ""
    errors: list[str] = field(default_factory=list)
    detail: Optional[str] = None
    attempts: int = 0
