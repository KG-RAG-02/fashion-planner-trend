"""LLM 백엔드 2종 — `codex_cli`(기본, 구독 Codex CLI) · `openai_api`(선택, LLM_API_KEY가 있을 때).

각 백엔드는 `run(prompt, json_schema, *, name, model, reasoning_effort, timeout_s) -> BackendReply`를 낸다.
여기서는 **JSON 파싱까지만** 하고, 스키마 일치·pydantic 검증은 `client`가 한다.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from .errors import LLMBackendError, LLMOutputError, LLMTimeout

__all__ = ["BackendReply", "CodexCLIBackend", "OpenAIAPIBackend", "parse_codex_events", "TOOL_ITEM_TYPES"]

# 스키마 JSON만 내야 하는 호출에서 꺼 두는 기능(`codex features list` 0.166.0 기준 이름).
# 받지 않는 이름이 있으면 스모크에서 빼고 기록한다.
DEFAULT_DISABLED_FEATURES: tuple[str, ...] = (
    "shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "image_generation", "view_image", "skill_search", "tool_suggest",
    "sleep_tool", "goals", "hooks",
)
DEFAULT_CONFIG_OVERRIDES: tuple[str, ...] = ('web_search="disabled"',)

# `--json` 이벤트의 item.type 중 "도구 사용"으로 세는 것 (에이전트 메시지·추론은 제외)
TOOL_ITEM_TYPES = {
    "command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call", "function_call",
    "custom_tool_call", "local_shell_call", "image_generation", "computer_call", "browser_action",
}
NON_TOOL_ITEM_TYPES = {"agent_message", "reasoning", "user_message", "todo_list", "error", "plan"}


@dataclass
class BackendReply:
    data: Any
    raw_text: str
    latency_s: float
    thread_id: Optional[str] = None
    usage: Optional[dict] = None
    tool_events: int = 0
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


def parse_codex_events(stdout: str) -> dict:
    """`codex exec --json` JSONL 이벤트 요약: thread_id·usage·도구 이벤트·오류·마지막 에이전트 메시지."""
    out: dict[str, Any] = {"thread_id": None, "usage": None, "tool_events": 0, "tool_types": [],
                           "errors": [], "last_message": None, "event_types": {}, "unparsed": 0}
    tool_ids: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            out["unparsed"] += 1
            continue
        if not isinstance(ev, dict):
            continue
        et = str(ev.get("type", ""))
        out["event_types"][et] = out["event_types"].get(et, 0) + 1
        if et == "thread.started":
            out["thread_id"] = ev.get("thread_id") or out["thread_id"]
        elif et == "turn.completed":
            if isinstance(ev.get("usage"), dict):
                out["usage"] = ev["usage"]
        elif et in ("turn.failed", "error"):
            err = ev.get("error") if isinstance(ev.get("error"), dict) else ev
            msg = err.get("message") if isinstance(err, dict) else None
            out["errors"].append(str(msg or err)[:500])
        elif et.startswith("item."):
            item = ev.get("item") or {}
            itype = str(item.get("type") or item.get("item_type") or "")
            iid = str(item.get("id") or f"{et}:{len(tool_ids)}")
            if itype == "agent_message" and et == "item.completed":
                out["last_message"] = item.get("text")
            elif itype and itype not in NON_TOOL_ITEM_TYPES and iid not in tool_ids:
                # 알 수 없는 item 종류도 도구 사용으로 센다(보수적) — 스모크에서 이름을 확인한다
                tool_ids.add(iid)
                out["tool_events"] += 1
                out["tool_types"].append(itype)
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


class CodexCLIBackend:
    """`codex exec`(구독)로 스키마 JSON 한 개를 받는다. 프롬프트는 stdin, 작업 폴더는 빈 임시 폴더."""

    name = "codex_cli"

    def __init__(self, executable: str = "codex", *, disabled_features: Sequence[str] = DEFAULT_DISABLED_FEATURES,
                 config_overrides: Sequence[str] = DEFAULT_CONFIG_OVERRIDES, ignore_user_config: bool = True,
                 extra_args: Sequence[str] = ()):
        self.executable = executable
        self.disabled_features = tuple(disabled_features)
        self.config_overrides = tuple(config_overrides)
        self.ignore_user_config = ignore_user_config
        self.extra_args = tuple(extra_args)

    def command(self, *, workdir: Path, schema_path: Path, out_path: Path, model: str,
                reasoning_effort: Optional[str]) -> list[str]:
        cmd = [self.executable, "exec", "--ephemeral", "--skip-git-repo-check"]
        if self.ignore_user_config:
            cmd += ["--ignore-user-config", "--ignore-rules"]
        cmd += ["-s", "read-only", "-C", str(workdir), "-m", model]
        if reasoning_effort:
            cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        for c in self.config_overrides:
            cmd += ["-c", c]
        for feat in self.disabled_features:
            cmd += ["--disable", feat]
        cmd += list(self.extra_args)
        cmd += ["--output-schema", str(schema_path), "-o", str(out_path), "--json", "-"]
        return cmd

    def run(self, prompt: str, json_schema: dict, *, name: str, model: str, reasoning_effort: Optional[str],
            timeout_s: float) -> BackendReply:
        with tempfile.TemporaryDirectory(prefix="trend-llm-") as tmp:
            tmpd = Path(tmp)
            work = tmpd / "work"
            work.mkdir()
            schema_path, out_path = tmpd / "schema.json", tmpd / "out.json"
            schema_path.write_text(json.dumps(json_schema, ensure_ascii=False), encoding="utf-8")
            cmd = self.command(workdir=work, schema_path=schema_path, out_path=out_path, model=model,
                               reasoning_effort=reasoning_effort)
            t0 = time.monotonic()
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", start_new_session=True)
            except FileNotFoundError:
                raise LLMBackendError(f"codex 실행 파일을 찾을 수 없음: {self.executable}") from None
            try:
                stdout, stderr = proc.communicate(prompt, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                raise LLMTimeout(f"codex exec 시간 초과 ({timeout_s:.0f}s)") from None
            latency = time.monotonic() - t0
            ev = parse_codex_events(stdout or "")
            extra = {"returncode": proc.returncode, "event_types": ev["event_types"], "stderr_tail": (stderr or "")[-800:]}
            if proc.returncode != 0:
                detail = "; ".join(ev["errors"]) or (stderr or "").strip()[-500:]
                exc = LLMBackendError(f"codex exec 비정상 종료 {proc.returncode}: {detail}")
                exc.partial = {"thread_id": ev["thread_id"], "latency_s": latency, **extra}  # type: ignore[attr-defined]
                raise exc
            text = out_path.read_text(encoding="utf-8") if out_path.exists() else (ev["last_message"] or "")
            if not text.strip():
                detail = "; ".join(ev["errors"])
                raise LLMOutputError(f"codex 출력이 비어 있음{': ' + detail if detail else ''}")
            data = _parse_json_text(text)
            return BackendReply(data=data, raw_text=text, latency_s=latency, thread_id=ev["thread_id"],
                                usage=ev["usage"], tool_events=ev["tool_events"], tool_types=ev["tool_types"],
                                extra=extra)


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
        import requests  # 지연 import — codex_cli만 쓰는 경로에서 필요 없게

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
