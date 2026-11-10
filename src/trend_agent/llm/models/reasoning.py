"""추론 모델 계열 — 예 gpt-5 계열, o 계열 (docs/pipeline.md D-30, §6.4).

추론 모델은 기본값 외의 `temperature`를 거부하므로 보내지 않는다. 추론 강도(`reasoning_effort`)는 주면 요청에 넣고,
주지 않으면 API 기본값을 쓴다. 허용 강도는 등록표의 `efforts`(없으면 `DEFAULT_EFFORTS`)다.
추론 토큰은 출력 토큰(`usage.completion_tokens`)에 포함돼 과금되고 `completion_tokens_details.reasoning_tokens`로 따로 보인다.
"""
from __future__ import annotations

from typing import Optional

from ..errors import LLMBackendError

NAME = "reasoning"
DEFAULT_EFFORTS = ("minimal", "low", "medium", "high")


def check_effort(spec, effort: Optional[str]) -> Optional[str]:
    if effort is None or effort == "":
        return None
    allowed = tuple(spec.efforts) or DEFAULT_EFFORTS
    if effort not in allowed:
        raise LLMBackendError(f"reasoning_effort는 {allowed} 중 하나여야 한다({spec.name}): {effort!r}")
    return effort


def request_params(effort: Optional[str]) -> dict:
    return {"reasoning_effort": effort} if effort else {}
