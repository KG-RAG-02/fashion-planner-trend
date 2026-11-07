"""LLM 호출 단일 진입점 — 계약 v1.1 (L0 작성, G1). 구현: T1 (`client.py`·`backends.py`·`schema.py`·`cache.py`).

문서: docs/spec.md §7.2 · docs/schemas/README.md 「계약 v1.1」. 사용자 결정 Q1′: 기본 백엔드는 구독 Codex CLI,
API는 옵션이다.

- 기본 백엔드 `codex_cli`(`codex exec --ephemeral --skip-git-repo-check -s read-only -m <고정 모델> --output-schema …`),
  선택 `openai_api`(LLM_API_KEY가 있을 때, temperature 0, 기존 `requests`로 구현 — 새 의존성 없음).
- 반환은 **검증 전 JSON dict**다. 호출자가 pydantic으로 다시 검증한다. **LLM은 숫자를 내지 않는다** — 스키마에 수치
  필드를 두지 않고 등급만 받는다(수치화는 코드·weights.yaml).
- 캐시 키 = (입력 sha256, backend, model, reasoning_effort, prompt_version). 호출 로그는 env `LLM_CALL_LOG`.
- 고정 모델 원칙: `model`도 env `LLM_MODEL`도 없으면 오류다.

공개 이름과 시그니처 (구현은 `client.py`·`schema.py`):

    def structured(prompt: str, json_schema: dict, *, purpose: str, prompt_version: str,
                   backend: str | None = None,          # 기본 env LLM_BACKEND, 없으면 "codex_cli"
                   model: str | None = None,            # 기본 env LLM_MODEL
                   reasoning_effort: str | None = None, # 기본 env LLM_REASONING_EFFORT (codex_cli만)
                   timeout_s: float | None = None, use_cache: bool = True) -> dict
    def call(<structured와 같은 인자>) -> LLMResult      # .data(dict) + .meta(backend·model·thread_id·schema_sha256·
                                                        #  input_sha256·latency_s·usage·cache_hit·tool_events·ok·error)
    def structured_model(prompt: str, model_cls: type[BaseModel], *, purpose: str, prompt_version: str,
                         exclude: dict[str, set[str]] | None = None, **kw) -> BaseModel  # strict 변환 + pydantic 재검증
    def strict_schema(model_cls, *, exclude=None, overrides=None) -> tuple[dict, list[str]]  # (스키마, 빠진 제약 목록)
    def schema_sha256(schema: dict) -> str

    오류: LLMError ⊃ LLMBackendError(비정상 종료·인증) · LLMOutputError(JSON 아님·스키마 불일치) · LLMTimeout
          (T3 Tier4 steps 코드 llm_unavailable·llm_invalid_output과 1:1 대응)

`purpose` 예: "article_extraction", "tier4_profile". 이 파일은 계약이라 L0만 고친다(바꿀 때는 contract_change).
구현이 들어오기 전에도 `import trend_agent.llm`은 성공하고, 이름을 쓰는 순간 구현 모듈을 불러온다(지연 로딩).
"""
from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "structured", "structured_model", "call", "LLMResult", "strict_schema", "schema_sha256",
    "LLMError", "LLMBackendError", "LLMOutputError", "LLMTimeout",
]

_SOURCE = {name: ".client" for name in __all__}
_SOURCE.update({"strict_schema": ".schema", "schema_sha256": ".schema"})


def __getattr__(name: str) -> Any:
    source = _SOURCE.get(name)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(source, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
