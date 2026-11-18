"""기사 추출 프롬프트 렌더링 — 템플릿 `prompts/article_extraction.md`(앞머리 YAML 제거) + 기사 메타 + 블록.

블록 줄 형식(번호 = block idx, LLM은 이 번호 하나만 쓴다):
    [3] 문단: …
    [4] 소제목: …
    [5] 이미지 (run 2 · two_column): 캡션 "Chanel 2027 S/S RTW" → 브랜드 Chanel · 시즌 2027SS
    [6] 이미지 (run 2 · two_column): 캡션: 사진 출처만
이미지 픽셀·URL은 보내지 않는다(링크만, 범위는 텍스트).

기사 헤더에는 크롤러가 찾은 시즌 후보(`season_candidates`)를 넣는다.
프롬프트는 ID(`article-extraction`)와 보내는 본문의 sha256(`prompt_sha256`)으로 식별한다.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ...config import ROOT

PROMPT_DIR = ROOT / "prompts"
PROMPT_ID = "article-extraction"
PROMPT_FILES = {PROMPT_ID: "article_extraction.md"}
TEXT_TYPES = {"paragraph": "문단", "heading": "소제목"}

__all__ = ["load_template", "prompt_sha256", "render_blocks", "render_prompt", "render_retry_suffix", "PROMPT_DIR", "PROMPT_ID"]


@lru_cache(maxsize=8)
def load_template(prompt_version: str = PROMPT_ID, prompt_dir: Optional[str] = None) -> tuple[str, dict]:
    """프롬프트 ID → (본문, 앞머리 dict). 모르는 ID거나 앞머리 prompt_id가 다르면 오류."""
    base = Path(prompt_dir) if prompt_dir else PROMPT_DIR
    name = PROMPT_FILES.get(prompt_version)
    if name is None:
        raise ValueError(f"알 수 없는 프롬프트 ID: {prompt_version!r} (지원: {', '.join(PROMPT_FILES)})")
    text = (base / name).read_text(encoding="utf-8")
    front: dict[str, Any] = {}
    fm = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if fm:
        front = yaml.safe_load(fm.group(1)) or {}
        text = text[fm.end():]
    if front.get("prompt_id") not in (None, prompt_version):
        raise ValueError(f"템플릿 앞머리 prompt_id {front.get('prompt_id')} != {prompt_version}")
    return text.strip() + "\n", front


def prompt_sha256(prompt_version: str = PROMPT_ID, prompt_dir: Optional[str] = None) -> str:
    """LLM에 보내는 템플릿 본문(앞머리 제외)의 sha256. 앞머리만 고치면 바뀌지 않는다."""
    body, _ = load_template(prompt_version, prompt_dir)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _one_line(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _image_line(b: dict) -> str:
    head = f"[{b['idx']}] 이미지 (run {b.get('run_idx')} · {b.get('layout') or '?'})"
    cap = _one_line(b.get("caption"))
    cp = b.get("caption_parsed") or {}
    ctype = cp.get("type")
    if not cap or ctype == "none":
        return f"{head}: 캡션 없음"
    if ctype == "credit_only":
        return f"{head}: 캡션: 사진 출처만"
    tail = []
    if cp.get("brand"):
        tail.append(f"브랜드 {cp['brand']}")
    if cp.get("season"):
        tail.append(f"시즌 {cp['season']}")
    suffix = f" → {' · '.join(tail)}" if tail else ""
    return f'{head}: 캡션 "{cap}"{suffix}'


def render_blocks(blocks: Iterable[dict]) -> str:
    lines = []
    for b in blocks:
        t = b.get("type")
        if t == "image":
            lines.append(_image_line(b))
        elif t in TEXT_TYPES:
            lines.append(f"[{b['idx']}] {TEXT_TYPES[t]}: {_one_line(b.get('text'))}")
    return "\n".join(lines)


_SEASON_SOURCES = (("title", "제목"), ("tag", "태그"), ("body", "본문"), ("caption", "캡션"))


def _season_candidate_lines(candidates: Optional[list[dict]]) -> list[str]:
    """크롤러의 `season_candidates`(값·원문·출처·횟수) → 헤더 줄. 같은 표준 값은 출처별 횟수를 합친다.

    표준 시즌으로 정규화하지 못한 표현(리조트 등)은 원문 그대로 두고 그렇다고 적는다. 많이 나온 값이 앞,
    같으면 값 문자열 순이라 같은 입력이면 같은 문자열이다(캐시 키가 안정적이다).
    """
    groups: dict[str, dict[str, int]] = {}
    for c in candidates or []:
        key = c.get("value") or f"{_one_line(c.get('raw'))} (표준 시즌으로 정규화 불가)"
        by = groups.setdefault(key, {})
        src = str(c.get("source") or "?")
        by[src] = by.get(src, 0) + int(c.get("count") or 1)
    if not groups:
        return ["- 시즌 후보: 없음"]
    known = dict(_SEASON_SOURCES)
    rows = ["- 시즌 후보 (크롤러가 찾은 시즌 표현. 힌트일 뿐이고 과거 컬렉션 인용도 섞여 있다):"]
    for key in sorted(groups, key=lambda k: (-sum(groups[k].values()), k)):
        by = groups[key]
        parts = [f"{ko} {by[s]}회" for s, ko in _SEASON_SOURCES if s in by]
        parts += [f"{s} {n}회" for s, n in by.items() if s not in known]
        rows.append(f"  - {key} ← {' · '.join(parts)}")
    return rows


def _article_header(article: dict) -> str:
    season = article.get("season") or {}
    rows = [
        f"- article_id: {article.get('article_id')}",
        f"- 제목: {_one_line(article.get('title'))}",
        f"- 발행일: {str(article.get('published_at') or '')[:10]}",
    ]
    if article.get("tags"):
        rows.append(f"- 태그: {', '.join(map(str, article['tags']))}")
    if season.get("value"):
        rows.append(f"- 기사 시즌 태그: {season['value']} (출처 {season.get('source')})")
    rows += _season_candidate_lines(article.get("season_candidates"))
    return "\n".join(rows)


def render_prompt(article: dict, prompt_version: str = PROMPT_ID, *, prompt_dir: Optional[str] = None) -> str:
    template, _front = load_template(prompt_version, str(prompt_dir) if prompt_dir else None)
    return (
        f"{template}\n"
        "## 8. 기사\n"
        f"{_article_header(article)}\n\n"
        "### 블록 (번호 = block_idx)\n"
        f"{render_blocks(article.get('blocks') or [])}\n\n"
        "위 기사에서 규칙대로 추출한 JSON 객체 하나만 출력하라.\n"
    )


def render_retry_suffix(errors: list[str], limit: int = 30) -> str:
    shown = errors[:limit]
    more = f"\n- … 외 {len(errors) - limit}건" if len(errors) > limit else ""
    return (
        "\n## 9. 이전 출력의 검증 오류 (다시 추출하라)\n"
        "이전 출력이 아래 코드 검사에 걸렸다. 같은 규칙으로 처음부터 다시 추출하되 이 오류가 없게 하라. "
        "인용(quote)과 *_raw는 해당 블록 원문을 글자 그대로 복사하고, Item 근거 블록은 1~2개의 인접 블록만 쓴다. "
        "맞출 수 없는 Item·속성은 빼거나 null로 둔다.\n"
        + "\n".join(f"- {e}" for e in shown) + more + "\n"
    )
