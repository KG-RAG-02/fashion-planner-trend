"""LLM 오류. 예외의 `.meta`에는 실패한 호출의 로그 레코드(원문 없음)가 붙는다."""
from __future__ import annotations

from typing import Optional


class LLMError(RuntimeError):
    meta: Optional[dict] = None


class LLMBackendError(LLMError):
    """백엔드 비정상 종료·인증·설정 오류(모델 미지정, 키 없음 등)."""


class LLMOutputError(LLMError):
    """응답이 JSON이 아니거나 스키마·pydantic 검증에 맞지 않음."""


class LLMTimeout(LLMError):
    """시간 초과."""
