"""일반(비추론) 모델 계열 — 예 gpt-4o-mini, gpt-4.1 (docs/pipeline.md D-30, §6.4).

결정적 출력을 위해 `temperature = 0`을 보낸다. 추론 강도(`reasoning_effort`)는 받지 않는다 — 주면 조용히 무시하지 않고 오류다.
"""
from __future__ import annotations

from typing import Optional

from ..errors import LLMBackendError

NAME = "standard"


def check_effort(spec, effort: Optional[str]) -> Optional[str]:
    if effort:
        raise LLMBackendError(f"일반 모델({spec.name})에는 reasoning_effort를 줄 수 없다")
    return None


def request_params(effort: Optional[str]) -> dict:
    return {"temperature": 0}
