"""모델 등록표와 모델 계열 모듈 (docs/pipeline.md D-30, §14.2) — 실제 LLM 호출 없음."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from fashion_planner_trend import llm
from fashion_planner_trend.llm import client, models
from fashion_planner_trend.llm.backends import BackendReply

ROOT = Path(__file__).resolve().parents[1]


def test_registry_file_lists_only_checked_models():
    reg = models.load_registry()
    assert reg["gpt-4o-mini"].profile == "standard" and reg["gpt-4.1"].profile == "standard"
    assert all(s.checked_at for s in reg.values())                       # 등록표 항목은 동작 확인일이 있어야 한다
    assert reg["gpt-5-mini"].profile == "reasoning" and "medium" in reg["gpt-5-mini"].efforts  # 합성 확인 뒤 등록(2026-11-10)
    assert models.get_profile("gpt-4o-mini").NAME == "standard"


def test_unregistered_model_is_an_error_even_for_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "load_env", lambda: {})
    with pytest.raises(llm.LLMBackendError, match="등록되지 않은 모델"):
        client.resolve_settings(None, "gpt-9-unknown", None, env={})
    with pytest.raises(llm.LLMBackendError, match="등록되지 않은 모델"):
        llm.call("p", {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                 purpose="unit", prompt_version="v0", model="gpt-9-unknown", cache_dir=tmp_path)


def test_profiles_request_params_and_effort_rules():
    std, rsn = models.get_profile("gpt-4.1"), models.get_profile("gpt-5-mini")   # gpt-5-mini는 conftest 임시 등록
    assert std.request_params(None) == {"temperature": 0}
    assert rsn.request_params(None) == {} and rsn.request_params("high") == {"reasoning_effort": "high"}
    with pytest.raises(llm.LLMBackendError, match="일반 모델"):
        std.check_effort(models.get_spec("gpt-4.1"), "low")
    spec = models.register_model("tmp-reasoning", "reasoning", efforts=["low", "medium"])
    try:
        assert rsn.check_effort(spec, "low") == "low" and rsn.check_effort(spec, None) is None
        with pytest.raises(llm.LLMBackendError, match="reasoning_effort"):
            rsn.check_effort(spec, "high")                                # 그 모델의 efforts 밖
    finally:
        models.unregister_model("tmp-reasoning")


@pytest.mark.parametrize("body,err", [
    ("models:\n  x:\n    profile: magic\n    checked_at: '2026-01-01'\n", "profile"),
    ("models:\n  x:\n    profile: standard\n    efforts: [low]\n    checked_at: '2026-01-01'\n", "efforts"),
    ("models:\n  x:\n    profile: standard\n", "checked_at"),
])
def test_registry_file_validation(tmp_path, body, err):
    p = tmp_path / "models.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(llm.LLMBackendError, match=err):
        models.load_registry(str(p))


def test_runtime_registration_does_not_touch_file():
    before = (ROOT / "configs" / "llm_models.yaml").read_bytes()
    models.register_model("tmp-std", "standard")
    try:
        assert models.get_spec("tmp-std").source == "runtime"
    finally:
        models.unregister_model("tmp-std")
    with pytest.raises(llm.LLMBackendError):
        models.get_spec("tmp-std")
    assert (ROOT / "configs" / "llm_models.yaml").read_bytes() == before


def test_check_llm_model_script_registers_temporarily(monkeypatch, tmp_path, capsys):
    """합성 확인 스크립트: 미등록 모델은 프로세스 안에서만 임시 등록하고, 끝나면 해제한다. 캐시를 쓰지 않는다."""
    monkeypatch.setattr(client, "load_env", lambda: {})
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    sent = []

    class _Fake:
        def run(self, prompt, schema, *, name, model, reasoning_effort, timeout_s, request_params=None):
            sent.append((name, model, request_params))
            return BackendReply(data={"colors": ["와인색"], "garments": ["코트"], "pattern": None}, raw_text="{}",
                                latency_s=0.2, thread_id="t", usage={"prompt_tokens": 50, "completion_tokens": 20})

    spec = importlib.util.spec_from_file_location("check_llm_model", ROOT / "scripts" / "check_llm_model.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client.register_backend("openai_api", _Fake())
    try:
        code = mod.main(["--model", "new-reasoner", "--profile", "reasoning", "--reasoning-effort", "low"])
        assert mod.main(["--model", "another-new"]) == 1                 # 미등록인데 --profile 없음 → 실패
    finally:
        client.register_backend("openai_api", None)
    out = capsys.readouterr().out
    assert code == 0 and "통과: new-reasoner (reasoning" in out and 'checked_at: "' in out
    assert sent == [("model_check", "new-reasoner", {"reasoning_effort": "low"})]
    with pytest.raises(llm.LLMBackendError):                               # 끝나면 임시 등록 해제
        models.get_spec("new-reasoner")
    assert not (tmp_path / "cache").exists()                               # 캐시를 쓰지 않는다
