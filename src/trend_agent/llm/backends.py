"""LLM 백엔드 — `openai_api`(OpenAI Chat Completions, 기본이자 유일). 키는 `.env`의 `LLM_API_KEY`다.

백엔드는 `run(prompt, json_schema, *, name, model, reasoning_effort, timeout_s) -> BackendReply`를 낸다.
여기서는 **JSON 파싱까지만** 하고, 스키마 일치·pydantic 검증은 `client`가 한다.
`reasoning_effort`는 인터페이스 호환용이고 `openai_api`는 쓰지 않는다(항상 None).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import LLMBackendError, LLMOutputError, LLMTimeout

__all__ = ["BackendReply", "OpenAIAPIBackend"]


@dataclass
class BackendReply:
    data: Any
    raw_text: str
    latency_s: float
    thread_id: Optional[str] = None
    usage: Optional[dict] = None
    tool_events: int = 0  # 도구를 쓰지 않는 백엔드라 항상 0 (기록 형식 호환용)
    tool_types: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _parse_json_text(text: str) -> Any:
    """응답 텍스트를 JSON으로 읽는다. 코드 블록(```json … ```)으로 감싼 경우만 벗겨 준다."""
    s = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", s, flags=re.S)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise LLMOutputError(f"응답이 JSON이 아님: {e.msg} (pos {e.pos}, len {len(s)})") from None


class OpenAIAPIBackend:
    """OpenAI Chat Completions + `response_format: json_schema(strict)`, temperature 0. 키 값은 로그·예외에 넣지 않는다."""

    name = "openai_api"
    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: Optional[str], *, url: Optional[str] = None, session: Any = None):
        self._api_key = api_key or ""
        self.url = url or self.URL
        self._session = session

    def __repr__(self) -> str:
        return f"OpenAIAPIBackend(url={self.url!r}, api_key=***)"

    def run(self, prompt: str, json_schema: dict, *, name: str, model: str, reasoning_effort: Optional[str],
            timeout_s: float) -> BackendReply:
        if not self._api_key:
            raise LLMBackendError("openai_api: LLM_API_KEY가 없다")
        import requests  # 지연 import

        sess = self._session or requests
        body = {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or "output",
                                                "schema": json_schema, "strict": True}},
        }
        t0 = time.monotonic()
        try:
            resp = sess.post(self.url, json=body, timeout=timeout_s,
                             headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"})
        except requests.Timeout:
            raise LLMTimeout(f"openai_api 시간 초과 ({timeout_s:.0f}s)") from None
        except requests.RequestException as e:
            raise LLMBackendError(f"openai_api 요청 실패: {type(e).__name__}") from None
        latency = time.monotonic() - t0
        if resp.status_code != 200:
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            raise LLMBackendError(f"openai_api HTTP {resp.status_code}: {str(msg)[:300]}")
        try:
            payload = resp.json()
            choice = payload["choices"][0]
            msg = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMOutputError("openai_api 응답 형식이 예상과 다름") from None
        if msg.get("refusal"):
            raise LLMOutputError(f"openai_api 거절: {str(msg['refusal'])[:200]}")
        text = msg.get("content") or ""
        if not text.strip():
            raise LLMOutputError(f"openai_api 출력이 비어 있음 (finish_reason={choice.get('finish_reason')})")
        data = _parse_json_text(text)
        return BackendReply(data=data, raw_text=text, latency_s=latency, thread_id=payload.get("id"),
                            usage=payload.get("usage"), extra={"finish_reason": choice.get("finish_reason")})
