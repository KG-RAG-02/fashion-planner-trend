"""llm/ 단위 테스트 — 실제 LLM 호출 없음. codex 이벤트는 합성 프롬프트 녹화본(tests/fixtures/llm/)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

import pytest
from pydantic import BaseModel, Field

import trend_agent.llm as llm
from trend_agent.llm import backends, client
from trend_agent.llm.backends import BackendReply, CodexCLIBackend, OpenAIAPIBackend, parse_codex_events
from trend_agent.llm.schema import check_instance, schema_sha256, strict_schema
from trend_agent.schemas.article_extraction import ArticleExtraction

FIX = Path(__file__).parent / "fixtures" / "llm"


# ---------- schema ----------
def _walk_objects(node, out):
    if isinstance(node, dict):
        if node.get("type") == "object":
            out.append(node)
        for v in node.values():
            _walk_objects(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_objects(v, out)
    return out


def test_strict_schema_all_objects_strict_and_no_refs():
    schema, dropped = strict_schema(
        ArticleExtraction, exclude={"ArticleExtraction": {"article_id"}, "ExtractedItem": {"size", "review_flags"}}
    )
    blob = json.dumps(schema)
    assert "$ref" not in blob and "$defs" not in blob and '"default"' not in blob
    objs = _walk_objects(schema, [])
    assert len(objs) >= 10
    for o in objs:
        assert o["additionalProperties"] is False
        assert o["required"] == list(o["properties"].keys())
    assert "article_id" not in schema["properties"]
    item = schema["properties"]["styles"]["items"]["properties"]["items"]["items"]
    assert "review_flags" not in item["properties"] and "size" not in item["properties"]
    # Optional enum → anyOf [enum, null]
    assert {"type": "null"} in item["properties"]["gender"]["anyOf"]
    # strict가 받지 않는 제약은 목록으로 돌려준다 (pydantic 재검증으로 보완)
    assert sorted(d.rsplit(".", 1)[-1] for d in dropped) == ["minItems=1", "minItems=1"]


def test_strict_schema_roundtrip_with_pydantic():
    schema, _ = strict_schema(ArticleExtraction, exclude={"ExtractedItem": {"size", "review_flags"}})
    item = {
        "name": "꽃 자수 재킷", "raw_text": "꽃을 수놓은 재킷", "gender": None, "category_raw": "재킷",
        "category_major": None, "category_minor": "재킷", "colors": [{"raw": "버터옐로우", "value": "옐로우"}],
        "materials": [], "patterns": [], "details": [{"raw": "꽃을 수놓은", "type": "detail", "value": "자수",
                                                      "motif": "꽃", "placement": None}],
        "styling_raw": None, "styling_accessory": None, "fit_raw": None, "fit": None, "shape_raw": None,
        "shape": None, "length_body_raw": None, "length_body": None, "length_sleeve_raw": None,
        "length_sleeve": None, "evidence": [{"block_idx": 3, "quote": "꽃을 수놓은 재킷"}], "image_links": [],
    }
    style = {"name": "절제된 맥시멀리즘", "name_origin": "article", "aliases": [], "seasons": [{"year": 2027, "season": "SS"}],
             "definition": "한 줄", "description": [], "focus_attributes": [], "evidence": [], "items": [item]}
    doc = {"article_id": "x1", "styles": [style]}
    assert check_instance(doc, schema) == []
    ext = ArticleExtraction.model_validate(doc)
    assert ext.styles[0].items[0].category_major.value == "아우터"  # 코드가 minor에서 도출


def test_check_instance_reports_violations():
    schema = {"type": "object", "properties": {"c": {"anyOf": [{"type": "string", "enum": ["레드"]}, {"type": "null"}]},
                                               "n": {"type": "integer"}},
              "required": ["c", "n"], "additionalProperties": False}
    assert check_instance({"c": None, "n": 1}, schema) == []
    errs = check_instance({"c": "보라", "n": True, "x": 1}, schema)
    joined = " ".join(errs)
    assert "anyOf" in joined and "$.n" in joined and "$.x" in joined
    assert any("필수" in e for e in check_instance({"c": None}, schema))


def test_schema_sha256_ignores_key_order():
    a = {"type": "object", "properties": {"a": {"type": "string"}}}
    b = {"properties": {"a": {"type": "string"}}, "type": "object"}
    assert schema_sha256(a) == schema_sha256(b)
    assert len(schema_sha256(a)) == 64


# ---------- codex 이벤트 ----------
def test_parse_codex_events_ok_fixture():
    ev = parse_codex_events((FIX / "codex_events_ok.jsonl").read_text(encoding="utf-8"))
    assert ev["thread_id"] and ev["tool_events"] == 0 and ev["errors"] == []
    assert ev["usage"]["input_tokens"] > 0
    assert json.loads(ev["last_message"])  # 마지막 agent_message = JSON


def test_parse_codex_events_counts_tool_once_per_item():
    ev = parse_codex_events((FIX / "codex_events_tool.jsonl").read_text(encoding="utf-8"))
    assert ev["tool_events"] == 1 and ev["tool_types"] == ["command_execution"]


def test_parse_codex_events_errors_and_garbage():
    text = 'not json\n{"type":"turn.failed","error":{"message":"usage limit reached"}}\n{"type":"error","message":"x"}\n'
    ev = parse_codex_events(text)
    assert ev["unparsed"] == 1 and ev["errors"] == ["usage limit reached", "x"]


class _FakePopen:
    """codex exec 대역: `-o` 파일을 쓰고 녹화 이벤트를 stdout으로 낸다."""

    stdout_text = ""
    out_text: Optional[str] = None
    returncode_value = 0
    hang = False
    last_cmd: list[str] = []
    last_input = ""

    def __init__(self, cmd, **kw):
        type(self).last_cmd = cmd
        self.cmd = cmd
        self.pid = 999999
        self.returncode = None

    def communicate(self, input=None, timeout=None):
        type(self).last_input = input
        if self.hang:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if self.out_text is not None:
            Path(self.cmd[self.cmd.index("-o") + 1]).write_text(self.out_text, encoding="utf-8")
        self.returncode = self.returncode_value
        return self.stdout_text, "stderr-tail"

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def fake_popen(monkeypatch):
    class P(_FakePopen):
        pass

    P.stdout_text = (FIX / "codex_events_ok.jsonl").read_text(encoding="utf-8")
    P.out_text = '{"color":"레드","items":["셔츠","바지"]}'
    monkeypatch.setattr(backends.subprocess, "Popen", P)
    monkeypatch.setattr(backends, "_kill_group", lambda proc: None)
    return P


SMALL = {"type": "object", "properties": {"color": {"anyOf": [{"type": "string", "enum": ["레드", "블루"]}, {"type": "null"}]},
                                          "items": {"type": "array", "items": {"type": "string"}}},
         "required": ["color", "items"], "additionalProperties": False}


def test_codex_backend_command_and_reply(fake_popen):
    r = CodexCLIBackend().run("프롬프트", SMALL, name="t", model="m-1", reasoning_effort="low", timeout_s=30)
    cmd = fake_popen.last_cmd
    assert cmd[:4] == ["codex", "exec", "--ephemeral", "--skip-git-repo-check"]
    for flag in ("--ignore-user-config", "--ignore-rules", "--output-schema", "--json"):
        assert flag in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only" and cmd[cmd.index("-m") + 1] == "m-1"
    assert 'model_reasoning_effort="low"' in cmd and cmd[-1] == "-"
    assert "shell_tool" in cmd and fake_popen.last_input == "프롬프트"
    assert r.data == {"color": "레드", "items": ["셔츠", "바지"]} and r.thread_id and r.tool_events == 0


def test_codex_backend_nonzero_exit(fake_popen):
    fake_popen.returncode_value = 2
    fake_popen.stdout_text = '{"type":"turn.failed","error":{"message":"usage limit reached"}}\n'
    with pytest.raises(llm.LLMBackendError, match="usage limit"):
        CodexCLIBackend().run("p", SMALL, name="t", model="m", reasoning_effort=None, timeout_s=30)


def test_codex_backend_timeout(fake_popen):
    fake_popen.hang = True
    with pytest.raises(llm.LLMTimeout):
        CodexCLIBackend().run("p", SMALL, name="t", model="m", reasoning_effort=None, timeout_s=1)


def test_codex_backend_not_json(fake_popen):
    fake_popen.out_text = "설명: 결과는 다음과 같다"
    with pytest.raises(llm.LLMOutputError, match="JSON"):
        CodexCLIBackend().run("p", SMALL, name="t", model="m", reasoning_effort=None, timeout_s=30)


def test_codex_backend_strips_code_fence(fake_popen):
    fake_popen.out_text = '```json\n{"color": null, "items": []}\n```'
    r = CodexCLIBackend().run("p", SMALL, name="t", model="m", reasoning_effort=None, timeout_s=30)
    assert r.data == {"color": None, "items": []}


# ---------- client ----------
class _FakeBackend:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def run(self, prompt, schema, *, name, model, reasoning_effort, timeout_s):
        self.calls += 1
        return BackendReply(data=self.data, raw_text=json.dumps(self.data), latency_s=0.5, thread_id="th-1",
                            usage={"input_tokens": 10}, tool_events=0)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "load_env", lambda: {})
    for k in ("LLM_BACKEND", "LLM_MODEL", "LLM_REASONING_EFFORT", "LLM_API_KEY", "LLM_CACHE_DIR", "LLM_CALL_LOG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    yield tmp_path
    client.register_backend("codex_cli", None)
    client.register_backend("openai_api", None)


def test_call_requires_fixed_model(isolated):
    with pytest.raises(llm.LLMBackendError, match="LLM_MODEL"):
        llm.structured("p", SMALL, purpose="t", prompt_version="v0")


def test_call_cache_and_log(isolated):
    fb = _FakeBackend({"color": "블루", "items": ["바지"]})
    client.register_backend("codex_cli", fb)
    kw = dict(purpose="unit", prompt_version="v0", model="m-1", reasoning_effort="low")
    r1 = llm.call("비밀 원문 프롬프트", SMALL, **kw)
    r2 = llm.call("비밀 원문 프롬프트", SMALL, **kw)
    assert fb.calls == 1 and r1.data == r2.data
    assert r1.meta["cache_hit"] is False and r2.meta["cache_hit"] is True and r2.meta["thread_id"] == "th-1"
    assert r1.meta["input_sha256"] == r2.meta["input_sha256"] and r1.meta["schema_sha256"] == schema_sha256(SMALL)
    files = list((isolated / "cache" / "codex_cli" / "m-1" / "low" / "v0").glob("*.json"))
    assert len(files) == 1
    log = (isolated / "calls.jsonl").read_text(encoding="utf-8")
    assert len(log.splitlines()) == 2 and "비밀 원문" not in log and "블루" not in log
    # 다른 prompt_version은 캐시를 공유하지 않는다
    llm.call("비밀 원문 프롬프트", SMALL, **{**kw, "prompt_version": "v1"})
    assert fb.calls == 2


def test_call_schema_mismatch_not_cached(isolated):
    fb = _FakeBackend({"color": "보라", "items": []})
    client.register_backend("codex_cli", fb)
    with pytest.raises(llm.LLMOutputError) as ei:
        llm.call("p", SMALL, purpose="unit", prompt_version="v0", model="m")
    assert ei.value.meta["ok"] is False and ei.value.meta["error_type"] == "LLMOutputError"
    assert not list((isolated / "cache").rglob("*.json"))
    rec = json.loads((isolated / "calls.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["ok"] is False


def test_reasoning_effort_only_for_codex(isolated):
    st = client.resolve_settings("openai_api", "gpt-x", "low", env={})
    assert st["reasoning_effort"] is None
    with pytest.raises(llm.LLMBackendError):
        client.resolve_settings("nope", "m", None, env={})


class _Tiny(BaseModel):
    name: str
    tags: list[str] = Field(min_length=1)


def test_structured_model_revalidates_and_forgets_cache(isolated):
    client.register_backend("codex_cli", _FakeBackend({"name": "a", "tags": []}))  # strict 통과, pydantic minItems 위반
    with pytest.raises(llm.LLMOutputError, match="pydantic"):
        llm.structured_model("p", _Tiny, purpose="unit", prompt_version="v0", model="m")
    assert not list((isolated / "cache").rglob("*.json"))
    client.register_backend("codex_cli", _FakeBackend({"name": "a", "tags": ["x"]}))
    out = llm.structured_model("p", _Tiny, purpose="unit", prompt_version="v0", model="m")
    assert isinstance(out, _Tiny) and out.tags == ["x"]


# ---------- openai_api ----------
class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class _Session:
    def __init__(self, resp):
        self.resp = resp
        self.sent = None

    def post(self, url, json=None, timeout=None, headers=None):
        self.sent = {"url": url, "json": json, "headers": headers}
        return self.resp


def test_openai_backend_request_shape():
    sess = _Session(_Resp(200, {"id": "chatcmpl-1", "usage": {"prompt_tokens": 5},
                                "choices": [{"message": {"content": '{"color":null,"items":[]}'}, "finish_reason": "stop"}]}))
    r = OpenAIAPIBackend("sk-test", session=sess).run("p", SMALL, name="article extraction", model="gpt-4o-mini",
                                                      reasoning_effort=None, timeout_s=10)
    body = sess.sent["json"]
    assert body["temperature"] == 0 and body["model"] == "gpt-4o-mini"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "article_extraction"
    assert r.data == {"color": None, "items": []} and r.thread_id == "chatcmpl-1"


def test_openai_backend_errors_hide_key():
    with pytest.raises(llm.LLMBackendError, match="LLM_API_KEY"):
        OpenAIAPIBackend(None).run("p", SMALL, name="t", model="m", reasoning_effort=None, timeout_s=1)
    sess = _Session(_Resp(401, {"error": {"message": "Incorrect API key"}}))
    with pytest.raises(llm.LLMBackendError) as ei:
        OpenAIAPIBackend("sk-secret-123", session=sess).run("p", SMALL, name="t", model="m", reasoning_effort=None,
                                                            timeout_s=1)
    assert "401" in str(ei.value) and "sk-secret-123" not in str(ei.value)
    assert "sk-secret" not in repr(OpenAIAPIBackend("sk-secret-123"))


def test_package_lazy_exports():
    assert llm.strict_schema is strict_schema and llm.LLMResult is client.LLMResult
    assert issubclass(llm.LLMTimeout, llm.LLMError)
