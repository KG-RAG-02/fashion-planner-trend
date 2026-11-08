"""LLM 응답 캐시와 호출 로그.

- 캐시 키 = (입력 sha256, backend, model, reasoning_effort, prompt_version) — 계약 v1.1.
  입력 sha256 = sha256(prompt + "\\n" + schema_sha256). 경로:
  `<cache_dir>/<backend>/<model>/<effort>/<prompt_version>/<input_sha256>.json`
- 캐시 파일 = {"request": {prompt, schema, …}, "response": dict, "meta": {...}} (로컬 산출물 — 커밋·Sync 금지)
- 호출 로그(JSONL, 1호출 1줄) = 해시·길이·지연·usage·thread·ok·error. **프롬프트·응답 원문은 넣지 않는다.**

위치(앞에서부터): 인자 → env `LLM_CACHE_DIR`/`LLM_CALL_LOG` → `$SHARED/extractions/{cache,calls.jsonl}` →
저장소 `data/processed/llm/{cache,calls.jsonl}`(gitignore 대상).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from ..config import ROOT

_LOG_LOCK = threading.Lock()
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def input_sha256(prompt: str, schema_sha: str) -> str:
    return hashlib.sha256((prompt + "\n" + schema_sha).encode("utf-8")).hexdigest()


def _safe(part: Optional[str]) -> str:
    s = _SAFE.sub("_", part or "none").strip("._")
    return s or "none"


def default_cache_dir(env: Optional[dict[str, str]] = None) -> Path:
    env = env if env is not None else dict(os.environ)
    if env.get("LLM_CACHE_DIR"):
        return Path(env["LLM_CACHE_DIR"])
    if env.get("SHARED"):
        return Path(env["SHARED"]) / "extractions" / "cache"
    return ROOT / "data" / "processed" / "llm" / "cache"


def default_call_log(env: Optional[dict[str, str]] = None) -> Path:
    env = env if env is not None else dict(os.environ)
    if env.get("LLM_CALL_LOG"):
        return Path(env["LLM_CALL_LOG"])
    if env.get("SHARED"):
        return Path(env["SHARED"]) / "extractions" / "calls.jsonl"
    return ROOT / "data" / "processed" / "llm" / "calls.jsonl"


class ResponseCache:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path(self, *, backend: str, model: str, reasoning_effort: Optional[str], prompt_version: str, key: str) -> Path:
        return self.root / _safe(backend) / _safe(model) / _safe(reasoning_effort) / _safe(prompt_version) / f"{key}.json"

    def get(self, **kw: Any) -> Optional[dict]:
        p = self.path(**kw)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None  # 깨진 캐시는 없는 것으로 본다(다시 호출해 덮어쓴다)

    def put(self, entry: dict, **kw: Any) -> Path:
        p = self.path(**kw)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return p


def append_log(path: Path | str, record: dict) -> None:
    """JSONL 한 줄 추가 (스레드 잠금 + 파일 잠금 — 동시 호출·여러 프로세스 안전)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _LOG_LOCK, open(p, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
