"""`$SHARED/terms/term_map.jsonl` — Term 계층의 원본(T3 소유 공유 형식, 계약 파일 아님). 문서: graph_schema.md §2.5·§3

행 종류 (`row`):
- `alias`   : 다른 이름·번역 → (article_id, style_norm)의 Style에 `ALIAS_OF`로 잇는다.
              키 = (norm, article_id, style_norm)
- `mention` : 트렌드어 언급 집계(T1 `term_mentions.jsonl` 파생). Style이 없을 수 있다. 키 = norm
- `profile` : Tier4 LLM 프로필(등급만, 숫자 없음) = **Tier4 캐시**. 키 = (norm, backend, model, prompt_version, context_sha)

그래프의 Term은 적재할 때마다 이 파일로 다시 만든다(`graph/terms.py`). 정규화는 계약 함수 `normalize_term()` 하나다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trend_agent.schemas.graph_schema import TermKind, TermOrigin, normalize_term, term_id

ROOT = Path(__file__).resolve().parents[3]
KST = timezone(timedelta(hours=9))


def default_path() -> Path:
    """env `TERM_MAP_PATH` → `$SHARED/terms/term_map.jsonl` → 저장소 `data/processed/terms/term_map.jsonl`(git 제외)."""
    if os.environ.get("TERM_MAP_PATH"):
        return Path(os.environ["TERM_MAP_PATH"])
    if os.environ.get("SHARED"):
        return Path(os.environ["SHARED"]) / "terms" / "term_map.jsonl"
    return ROOT / "data" / "processed" / "terms" / "term_map.jsonl"


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


class TermEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    article_id: str
    block_idx: Optional[int] = None
    role: Optional[str] = None


class TermMapRow(BaseModel):
    model_config = ConfigDict(extra="allow")  # 모르는 키는 보존한다(다른 세션이 덧붙인 정보)

    row: Literal["alias", "mention", "profile"]
    term: str
    norm: str = ""
    kind: TermKind
    origin: TermOrigin
    # alias
    article_id: Optional[str] = None
    style_norm: Optional[str] = None
    evidence: list[TermEvidence] = Field(default_factory=list)
    # mention
    article_count: Optional[int] = None
    # profile (Tier4 캐시)
    backend: Optional[str] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    context_sha: Optional[str] = None
    profile: Optional[dict[str, Any]] = None
    # 기록
    added_at: Optional[str] = None
    added_by: Optional[str] = None
    created_at: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "TermMapRow":
        n = normalize_term(self.term)
        if not self.norm:
            self.norm = n
        elif self.norm != n:
            raise ValueError(f"norm {self.norm!r} != normalize_term({self.term!r}) {n!r}")
        if not self.norm:
            raise ValueError("빈 용어")
        if self.row == "alias" and not self.style_norm:
            raise ValueError("alias 행에는 style_norm이 필요하다")
        if self.style_norm is not None and self.style_norm != normalize_term(self.style_norm):
            raise ValueError(f"style_norm은 정규화된 값이어야 한다: {self.style_norm!r}")
        if self.row == "profile":
            if self.origin != "llm" or not (self.backend and self.prompt_version and self.context_sha) or self.profile is None:
                raise ValueError("profile 행에는 origin=llm, backend, prompt_version, context_sha, profile이 필요하다")
        return self

    @property
    def term_id(self) -> str:
        return term_id(self.norm)

    @property
    def key(self) -> tuple:
        if self.row == "alias":
            return ("alias", self.norm, self.article_id, self.style_norm)
        if self.row == "mention":
            return ("mention", self.norm)
        return ("profile", self.norm, self.backend, self.model, self.prompt_version, self.context_sha)


def read_term_map(path: Optional[Path | str] = None) -> list[TermMapRow]:
    """없으면 빈 목록. 잘못된 줄은 줄 번호와 함께 오류를 낸다(조용히 버리지 않는다)."""
    p = Path(path) if path else default_path()
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(TermMapRow.model_validate_json(line))
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"{p}:{n}: {e}") from e
    return rows


def dump_row(row: TermMapRow) -> str:
    return json.dumps(row.model_dump(mode="json", exclude_none=True, exclude_defaults=False), ensure_ascii=False, sort_keys=False)


def append_rows(rows: Iterable[TermMapRow], path: Optional[Path | str] = None) -> int:
    """키가 이미 있는 행은 다시 쓰지 않는다(멱등). 반환: 새로 쓴 줄 수."""
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    have = {r.key for r in read_term_map(p)}
    new = []
    for r in rows:
        if r.key not in have:
            have.add(r.key)
            new.append(r)
    if new:
        with p.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(dump_row(r) + "\n")
    return len(new)


def find_profile(rows: Iterable[TermMapRow], norm: str, backend: str, model: Optional[str], prompt_version: str,
                 context_sha: str) -> Optional[TermMapRow]:
    """Tier4 캐시 조회. 같은 키가 여러 줄이면 마지막 줄."""
    key = ("profile", norm, backend, model, prompt_version, context_sha)
    hit = None
    for r in rows:
        if r.key == key:
            hit = r
    return hit


# ---------- 생성: T1 term_mentions.jsonl → mention·alias 행 ----------
def rows_from_mentions(mentions: Iterable[dict[str, Any]], added_at: Optional[str] = None,
                       include_alias: bool = False) -> list[TermMapRow]:
    """T1 `term_mentions.jsonl`(설계 노트 §4.7) → mention 행(용어마다 1, `generic=true` 제외) [+ alias 행].

    alias 행(include_alias=True일 때만): 근거 `role=style`이고 그 Style 이름의 정규화가 용어와 다를 때 → (article_id, style_norm).
    기본은 끈다 — 실데이터(extract-v2)에서 `퍼플→자수정 퍼플`·`스커트→치마`처럼 넓은 말이 좁은 Style에 이어져 Tier1을
    휴리스틱으로 넓히기 때문이다. 기사 안 다른 이름은 추출 `styles[].aliases`로 이미 ALIAS_OF가 된다(적재기).
    용어 정규화는 계약 normalize_term()으로 다시 계산한다(T1 `norm`과 다르면 계약 쪽을 쓴다).
    """
    at = added_at or now_kst()
    out: list[TermMapRow] = []
    for m in mentions:
        term = (m.get("term") or "").strip()
        if not normalize_term(term) or m.get("generic"):
            continue
        norm = normalize_term(term)
        out.append(TermMapRow(row="mention", term=term, kind="trend_word", origin="term_mentions",
                              article_count=m.get("article_count"), added_at=at, added_by="T3"))
        if not include_alias:
            continue
        seen: set[tuple[str, str]] = set()
        for ev in m.get("evidence") or []:
            sname = ev.get("style_name")
            if ev.get("role") != "style" or not sname:
                continue
            snorm = normalize_term(sname)
            k = (str(ev.get("article_id")), snorm)
            if not snorm or snorm == norm or k in seen:
                continue
            seen.add(k)
            out.append(TermMapRow(row="alias", term=term, kind="alias", origin="term_mentions", article_id=k[0], style_norm=snorm,
                                  evidence=[TermEvidence(article_id=k[0], block_idx=ev.get("block_idx"), role="style")],
                                  added_at=at, added_by="T3"))
    return out


def build(mentions_path: Path | str, path: Optional[Path | str] = None, include_alias: bool = False) -> dict[str, int]:
    """term_map.jsonl에 mention(·alias) 행을 더한다(멱등 — 같은 키는 다시 쓰지 않는다, 기존 profile 캐시는 보존)."""
    with Path(mentions_path).open(encoding="utf-8") as f:
        mentions = [json.loads(line) for line in f if line.strip()]
    rows = rows_from_mentions(mentions, include_alias=include_alias)
    written = append_rows(rows, path)
    return {"mentions_in": len(mentions), "rows_built": len(rows), "rows_written": written,
            "alias_rows": sum(1 for r in rows if r.row == "alias")}


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m trend_agent.resolver.term_map")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="T1 term_mentions.jsonl → term_map.jsonl (mention·alias 행 추가)")
    b.add_argument("--mentions", required=True)
    b.add_argument("--out", default=None, help="기본 $SHARED/terms/term_map.jsonl")
    b.add_argument("--aliases", action="store_true", help="role=style 근거로 alias 행도 만든다(기본 끔 — 휴리스틱)")
    a = ap.parse_args(argv)
    print(json.dumps(build(a.mentions, a.out, include_alias=a.aliases), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
