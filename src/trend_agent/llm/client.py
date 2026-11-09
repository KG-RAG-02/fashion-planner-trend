"""LLM 호출 단일 진입점 구현 (계약: `trend_agent/llm/__init__.py` docstring).

    call(prompt, json_schema, *, purpose, prompt_version, backend=None, model=None, reasoning_effort=None,
         timeout_s=None, use_cache=True) -> LLMResult
    structured(...) -> dict                       # call(...).data
    structured_model(prompt, model_cls, *, purpose, prompt_version, exclude=None, overrides=None, **kw) -> BaseModel

- 설정: 인자 → env(`LLM_MODEL`·`LLM_API_KEY`·`LLM_TIMEOUT_S`, 저장소 `.env` 포함). 백엔드는 `openai_api` 하나다.
  **모델이 정해지지 않으면 `LLMBackendError`**(고정 모델 원칙).
- 반환 dict는 strict 스키마 부분집합으로 검사한 것이다(불일치 → `LLMOutputError`). pydantic 재검증은 호출자 몫이고,
  `structured_model`이 대신 해 준다.
- 실패하면 예외를 던진다(`LLMResult.meta.ok`는 반환된 결과에서 항상 True). 실패 호출도 로그에 남고 예외 `.meta`에 붙는다.
- 캐시는 스키마 검사를 통과한 응답만 저장한다. `structured_model`에서 pydantic이 거부하면 그 캐시 항목을 지운다.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from ..config import load_env
from .backends import REASONING_EFFORTS, BackendReply, OpenAIAPIBackend, is_reasoning_model
from .cache import ResponseCache, append_log, default_cache_dir, default_call_log, input_sha256
from .errors import LLMBackendError, LLMError, LLMOutputError, LLMTimeout
from .schema import check_instance, schema_sha256, strict_schema

__all__ = [
    "structured", "structured_model", "call", "LLMResult",
    "LLMError", "LLMBackendError", "LLMOutputError", "LLMTimeout",
    "register_backend", "resolve_settings",
]

BACKENDS = ("openai_api",)
DEFAULT_TIMEOUT_S = 600.0
_registry: dict[str, Any] = {}


@dataclass
class LLMResult:
    data: dict
    meta: dict = field(default_factory=dict)


def register_backend(name: str, impl: Any) -> None:
    """백엔드 구현을 바꿔 끼운다(테스트·녹화 재생용). `impl.run(prompt, schema, name=, model=, reasoning_effort=, timeout_s=)`."""
    if impl is None:
        _registry.pop(name, None)
    else:
        _registry[name] = impl


def _env() -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(load_env())
    return merged


def resolve_settings(backend: Optional[str] = None, model: Optional[str] = None,
                     reasoning_effort: Optional[str] = None, timeout_s: Optional[float] = None,
                     env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    env = env if env is not None else _env()
    b = backend or "openai_api"
    if b not in BACKENDS:
        raise LLMBackendError(f"알 수 없는 백엔드: {b} (허용 {BACKENDS})")
    m = model or env.get("LLM_MODEL") or ""
    if not m:
        raise LLMBackendError("모델이 정해지지 않았다: model 인자나 env LLM_MODEL이 필요하다(고정 모델 원칙)")
    # 추론 강도는 추론 모델에만 (docs/pipeline.md D-30). 일반 모델에 주면 조용히 무시하지 않고 오류다
    eff = reasoning_effort or None
    if eff is not None and eff not in REASONING_EFFORTS:
        raise LLMBackendError(f"reasoning_effort는 {REASONING_EFFORTS} 중 하나: {eff!r}")
    if eff is not None and not is_reasoning_model(m):
        raise LLMBackendError(f"일반 모델({m})에는 reasoning_effort를 줄 수 없다")
    t = timeout_s if timeout_s is not None else float(env.get("LLM_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    return {"backend": b, "model": m, "reasoning_effort": eff, "timeout_s": t, "env": env}


def _backend_impl(name: str, env: dict[str, str]) -> Any:
    if name in _registry:
        return _registry[name]
    return OpenAIAPIBackend(env.get("LLM_API_KEY") or None)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def call(prompt: str, json_schema: dict, *, purpose: str, prompt_version: str, backend: Optional[str] = None,
         model: Optional[str] = None, reasoning_effort: Optional[str] = None, timeout_s: Optional[float] = None,
         use_cache: bool = True, cache_dir: Optional[Path | str] = None,
         call_log: Optional[Path | str] = None) -> LLMResult:
    st = resolve_settings(backend, model, reasoning_effort, timeout_s)
    env = st["env"]
    schema_sha = schema_sha256(json_schema)
    key = input_sha256(prompt, schema_sha)
    meta: dict[str, Any] = {
        "ts": _now(), "purpose": purpose, "prompt_version": prompt_version, "backend": st["backend"],
        "model": st["model"], "reasoning_effort": st["reasoning_effort"], "schema_sha256": schema_sha,
        "input_sha256": key, "prompt_chars": len(prompt), "thread_id": None, "latency_s": None, "usage": None,
        "cache_hit": False, "tool_events": 0, "tool_types": [], "ok": False, "error": None, "error_type": None,
    }
    cache = ResponseCache(cache_dir or default_cache_dir(env))
    ckey = {"backend": st["backend"], "model": st["model"], "reasoning_effort": st["reasoning_effort"],
            "prompt_version": prompt_version, "key": key}
    log_path = Path(call_log or default_call_log(env))

    if use_cache:
        hit = cache.get(**ckey)
        if hit is not None and isinstance(hit.get("response"), dict) and not check_instance(hit["response"], json_schema):
            old = hit.get("meta", {})
            meta.update(cache_hit=True, ok=True, latency_s=0.0, cached_latency_s=old.get("latency_s"),
                        thread_id=old.get("thread_id"), usage=None, tool_events=old.get("tool_events", 0),
                        tool_types=old.get("tool_types", []))
            append_log(log_path, meta)
            return LLMResult(data=hit["response"], meta=meta)

    impl = _backend_impl(st["backend"], env)
    t0 = time.monotonic()
    try:
        reply: BackendReply = impl.run(prompt, json_schema, name=purpose, model=st["model"],
                                       reasoning_effort=st["reasoning_effort"], timeout_s=st["timeout_s"])
        if not isinstance(reply.data, dict):
            raise LLMOutputError(f"최상위가 객체가 아님: {type(reply.data).__name__}")
        problems = check_instance(reply.data, json_schema)
        meta.update(thread_id=reply.thread_id, usage=reply.usage, latency_s=round(reply.latency_s, 3),
                    tool_events=reply.tool_events, tool_types=reply.tool_types)
        if problems:
            raise LLMOutputError(f"스키마 불일치 {len(problems)}건: " + " | ".join(problems[:5]))
    except LLMError as e:
        partial = getattr(e, "partial", None) or {}
        meta.update(error=str(e)[:800], error_type=type(e).__name__,
                    thread_id=meta["thread_id"] or partial.get("thread_id"),
                    latency_s=meta["latency_s"] or round(time.monotonic() - t0, 3))
        append_log(log_path, meta)
        e.meta = meta
        raise
    meta["ok"] = True
    append_log(log_path, meta)
    if use_cache:
        cache.put({"request": {"prompt": prompt, "schema": json_schema, "purpose": purpose},
                   "response": reply.data, "meta": meta, "raw_text": reply.raw_text}, **ckey)
    return LLMResult(data=reply.data, meta=meta)


def structured(prompt: str, json_schema: dict, *, purpose: str, prompt_version: str, **kw: Any) -> dict:
    return call(prompt, json_schema, purpose=purpose, prompt_version=prompt_version, **kw).data


def structured_model(prompt: str, model_cls: type[BaseModel], *, purpose: str, prompt_version: str,
                     exclude: Optional[dict[str, set[str]]] = None, overrides: Optional[dict] = None,
                     **kw: Any) -> BaseModel:
    """pydantic 모델 → strict 스키마 → 호출 → pydantic 재검증. 재검증 실패는 `LLMOutputError`(캐시 항목 삭제)."""
    schema, _dropped = strict_schema(model_cls, exclude=exclude, overrides=overrides)
    res = call(prompt, schema, purpose=purpose, prompt_version=prompt_version, **kw)
    try:
        return model_cls.model_validate(res.data)
    except ValidationError as e:
        _forget(res.meta, kw.get("cache_dir"))
        err = LLMOutputError(f"pydantic 재검증 실패: {e.error_count()}건 — {str(e)[:500]}")
        err.meta = res.meta
        raise err from None


def _forget(meta: dict, cache_dir: Optional[Path | str]) -> None:
    cache = ResponseCache(cache_dir or default_cache_dir(_env()))
    p = cache.path(backend=meta["backend"], model=meta["model"], reasoning_effort=meta["reasoning_effort"],
                   prompt_version=meta["prompt_version"], key=meta["input_sha256"])
    try:
        p.unlink()
    except FileNotFoundError:
        pass
