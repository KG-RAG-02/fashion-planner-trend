"""llm/ 단위 테스트 — 실제 LLM 호출 없음."""
from __future__ import annotations

import json
from typing import Optional

import pytest
from pydantic import BaseModel, Field

import trend_agent.llm as llm
from trend_agent.llm import backends, client
from trend_agent.llm.backends import BackendReply, OpenAIAPIBackend
from trend_agent.llm.schema import check_instance, schema_sha256, strict_schema
from trend_agent.schemas.article_extraction import ArticleExtraction


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


SMALL = {"type": "object", "properties": {"color": {"anyOf": [{"type": "string", "enum": ["레드", "블루"]}, {"type": "null"}]},
                                          "items": {"type": "array", "items": {"type": "string"}}},
         "required": ["color", "items"], "additionalProperties": False}


# ---------- client ----------
class _FakeBackend:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def run(self, prompt, schema, *, name, model, reasoning_effort, timeout_s, **kw):
        self.calls += 1
        return BackendReply(data=self.data, raw_text=json.dumps(self.data), latency_s=0.5, thread_id="th-1",
                            usage={"input_tokens": 10}, tool_events=0)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "load_env", lambda: {})
    for k in ("LLM_MODEL", "LLM_REASONING_EFFORT", "LLM_API_KEY", "LLM_CACHE_DIR", "LLM_CALL_LOG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    yield tmp_path
    client.register_backend("openai_api", None)


def test_call_requires_fixed_model(isolated):
    with pytest.raises(llm.LLMBackendError, match="LLM_MODEL"):
        llm.structured("p", SMALL, purpose="t", prompt_version="v0")


def test_call_cache_and_log(isolated):
    fb = _FakeBackend({"color": "블루", "items": ["바지"]})
    client.register_backend("openai_api", fb)
    kw = dict(purpose="unit", prompt_version="v0", model="m-1")
    r1 = llm.call("비밀 원문 프롬프트", SMALL, **kw)
    r2 = llm.call("비밀 원문 프롬프트", SMALL, **kw)
    assert fb.calls == 1 and r1.data == r2.data
    assert r1.meta["cache_hit"] is False and r2.meta["cache_hit"] is True and r2.meta["thread_id"] == "th-1"
    assert r1.meta["input_sha256"] == r2.meta["input_sha256"] and r1.meta["schema_sha256"] == schema_sha256(SMALL)
    files = list((isolated / "cache" / "openai_api" / "m-1" / "none" / "v0").glob("*.json"))
    assert len(files) == 1
    log = (isolated / "calls.jsonl").read_text(encoding="utf-8")
    assert len(log.splitlines()) == 2 and "비밀 원문" not in log and "블루" not in log
    # 다른 prompt_version은 캐시를 공유하지 않는다
    llm.call("비밀 원문 프롬프트", SMALL, **{**kw, "prompt_version": "v1"})
    assert fb.calls == 2


def test_call_schema_mismatch_not_cached(isolated):
    fb = _FakeBackend({"color": "보라", "items": []})
    client.register_backend("openai_api", fb)
    with pytest.raises(llm.LLMOutputError) as ei:
        llm.call("p", SMALL, purpose="unit", prompt_version="v0", model="m")
    assert ei.value.meta["ok"] is False and ei.value.meta["error_type"] == "LLMOutputError"
    assert not list((isolated / "cache").rglob("*.json"))
    rec = json.loads((isolated / "calls.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["ok"] is False


def test_openai_is_the_only_backend_and_effort_rules(isolated):
    st = client.resolve_settings(None, "gpt-4o-mini", None, env={})
    assert st["backend"] == "openai_api" and st["reasoning_effort"] is None
    with pytest.raises(llm.LLMBackendError, match="일반 모델"):            # D-30: 일반 모델에 추론 강도 → 오류
        client.resolve_settings(None, "gpt-4.1", "low", env={})
    assert client.resolve_settings(None, "gpt-5-mini", "medium", env={})["reasoning_effort"] == "medium"
    assert client.resolve_settings(None, "gpt-5-mini", None, env={})["reasoning_effort"] is None  # API 기본값
    with pytest.raises(llm.LLMBackendError, match="reasoning_effort"):
        client.resolve_settings(None, "gpt-5-mini", "max", env={})
    with pytest.raises(llm.LLMBackendError):
        client.resolve_settings("codex_cli", "m", None, env={})  # Codex CLI 백엔드는 없다
    with pytest.raises(llm.LLMBackendError):
        client.resolve_settings("nope", "m", None, env={})


def test_default_reasoning_effort_from_env_only_with_env_model(isolated):
    """D-40: 기본 모델·추론 강도는 .env(LLM_MODEL·LLM_REASONING_EFFORT). 강도 기본값은 모델도 env에서 올 때만 쓴다."""
    env = {"LLM_MODEL": "gpt-6-sol", "LLM_REASONING_EFFORT": "none"}
    st = client.resolve_settings(None, None, None, env=env)
    assert (st["model"], st["reasoning_effort"]) == ("gpt-6-sol", "none")
    assert st["request_params"] == {"reasoning_effort": "none"}
    assert client.resolve_settings(None, None, "medium", env=env)["reasoning_effort"] == "medium"  # 인자가 우선
    st = client.resolve_settings(None, "gpt-4.1", None, env=env)   # 모델을 인자로 주면 env 강도를 섞지 않는다
    assert (st["model"], st["reasoning_effort"]) == ("gpt-4.1", None)
    assert client.resolve_settings(None, "gpt-5-mini", None, env=env)["reasoning_effort"] is None
    with pytest.raises(llm.LLMBackendError, match="reasoning_effort"):  # env 강도도 등록표로 검사한다
        client.resolve_settings(None, None, None, env={"LLM_MODEL": "gpt-5-mini", "LLM_REASONING_EFFORT": "none"})


class _Tiny(BaseModel):
    name: str
    tags: list[str] = Field(min_length=1)


def test_structured_model_revalidates_and_forgets_cache(isolated):
    client.register_backend("openai_api", _FakeBackend({"name": "a", "tags": []}))  # strict 통과, pydantic minItems 위반
    with pytest.raises(llm.LLMOutputError, match="pydantic"):
        llm.structured_model("p", _Tiny, purpose="unit", prompt_version="v0", model="m")
    assert not list((isolated / "cache").rglob("*.json"))
    client.register_backend("openai_api", _FakeBackend({"name": "a", "tags": ["x"]}))
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
    """백엔드는 모델 규칙을 모른다: request_params(계열 모듈이 만든 것)를 요청 본문에 더할 뿐이다 (D-30)."""
    pytest.importorskip("requests")  # 데모 이미지에는 requests가 없다
    sess = _Session(_Resp(200, {"id": "chatcmpl-1", "usage": {"prompt_tokens": 5},
                                "choices": [{"message": {"content": '{"color":null,"items":[]}'}, "finish_reason": "stop"}]}))
    r = OpenAIAPIBackend("sk-test", session=sess).run("p", SMALL, name="article extraction", model="gpt-4o-mini",
                                                      reasoning_effort=None, timeout_s=10,
                                                      request_params={"temperature": 0})
    body = sess.sent["json"]
    assert body["temperature"] == 0 and body["model"] == "gpt-4o-mini"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "article_extraction"
    assert r.data == {"color": None, "items": []} and r.thread_id == "chatcmpl-1"
    sess = _Session(_Resp(200, {"id": "x", "choices": [{"message": {"content": "{}"}}]}))
    OpenAIAPIBackend("sk-test", session=sess).run("p", SMALL, name="t", model="any", reasoning_effort=None, timeout_s=1)
    assert "temperature" not in sess.sent["json"]                     # 파라미터를 받지 않으면 더하지 않는다
    with pytest.raises(llm.LLMBackendError, match="고정 필드"):        # 고정 필드(model 등)는 덮어쓸 수 없다
        OpenAIAPIBackend("sk-test", session=sess).run("p", SMALL, name="t", model="m", reasoning_effort=None,
                                                      timeout_s=1, request_params={"model": "other"})


def test_client_sends_profile_params_through_backend(isolated):
    """client가 등록표의 계열 모듈로 요청 파라미터를 만들어 백엔드에 넘긴다: 일반 = temperature 0, 추론 = reasoning_effort."""
    seen = []

    class _Rec:
        def run(self, prompt, schema, *, name, model, reasoning_effort, timeout_s, request_params=None):
            seen.append((model, reasoning_effort, request_params))
            return BackendReply(data={"color": None, "items": []}, raw_text="{}", latency_s=0.1, thread_id="t")

    client.register_backend("openai_api", _Rec())
    res = llm.call("p", SMALL, purpose="unit", prompt_version="v0", model="m", use_cache=False)
    assert seen[-1] == ("m", None, {"temperature": 0}) and res.meta["profile"] == "standard"
    llm.call("p", SMALL, purpose="unit", prompt_version="v0", model="gpt-5-mini", reasoning_effort="medium", use_cache=False)
    assert seen[-1] == ("gpt-5-mini", "medium", {"reasoning_effort": "medium"})
    llm.call("p", SMALL, purpose="unit", prompt_version="v0", model="gpt-5-mini", use_cache=False)
    assert seen[-1] == ("gpt-5-mini", None, {})                         # 강도를 주지 않으면 API 기본값


def test_openai_backend_errors_hide_key():
    pytest.importorskip("requests")
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
