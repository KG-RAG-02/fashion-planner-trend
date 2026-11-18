"""LLM 호출 단일 진입점. 구현: `client.py`·`backends.py`·`schema.py`·`cache.py`·`models/`.

문서: docs/spec.md §7.2 · docs/pipeline.md §14.2. 결정 D-1·D-16: OpenAI API를 유일한 백엔드로 쓴다(모델은 실험으로 고른다).

- 백엔드 `openai_api`(`LLM_API_KEY` 필요, OpenAI Chat Completions + strict JSON schema, 기존 `requests`로 구현).
- 모델은 `configs/llm_models.yaml`에 **등록된 것만** 쓴다. 요청 규칙은 모델 계열 모듈이 정한다 — `models/standard.py`
  (일반 모델, temperature 0), `models/reasoning.py`(추론 모델, temperature 없음, reasoning_effort). D-30.
- 반환은 **검증 전 JSON dict**다. 호출자가 pydantic으로 다시 검증한다. LLM은 숫자를 내지 않는다 — 스키마에 수치
  필드를 두지 않고 등급만 받는다(수치화는 코드·weights.yaml).
- 캐시 키 = (입력 sha256, backend, model, reasoning_effort, prompt_version). 호출 로그는 env `LLM_CALL_LOG`.
- 고정 모델 원칙: `model`도 env `LLM_MODEL`도 없으면 오류다 (코드에 모델명을 두지 않는다).

공개 이름과 시그니처 (구현은 `client.py`·`schema.py`):

    def structured(prompt: str, json_schema: dict, *, purpose: str, prompt_version: str,
                   backend: str | None = None,          # 기본 "openai_api"
                   model: str | None = None,            # 기본 env LLM_MODEL
                   reasoning_effort: str | None = None, # 추론 모델만 (minimal·low·medium·high, D-30). 일반 모델에 주면 오류
                   timeout_s: float | None = None, use_cache: bool = True) -> dict
    def call(<structured와 같은 인자>) -> LLMResult      # .data(dict) + .meta(backend·model·thread_id·schema_sha256·
                                                        #  input_sha256·latency_s·usage·cache_hit·ok·error)
    def structured_model(prompt: str, model_cls: type[BaseModel], *, purpose: str, prompt_version: str,
                         exclude: dict[str, set[str]] | None = None, **kw) -> BaseModel  # strict 변환 + pydantic 재검증
    def strict_schema(model_cls, *, exclude=None, overrides=None) -> tuple[dict, list[str]]  # (스키마, 빠진 제약 목록)
    def schema_sha256(schema: dict) -> str

    오류: LLMError ⊃ LLMBackendError(비정상 종료·인증·설정) · LLMOutputError(JSON 아님·스키마 불일치) · LLMTimeout

`purpose` 예: "article_extraction". 이 인터페이스를 바꾸면 docs/pipeline.md §14.2도 같이 고친다.
`import fashion_planner_trend.llm`은 구현 모듈을 읽지 않고, 이름을 쓰는 순간 불러온다(지연 로딩).
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
