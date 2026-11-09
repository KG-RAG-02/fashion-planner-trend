"""실험 실행기(scripts/run_experiment.py) — 합성 기사·가짜 백엔드, 실제 LLM 호출 없음."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

from trend_agent.llm import client

from test_extraction import ARTICLE, _item, _llm_doc
from test_extraction_batch import _ByArticle

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_experiment", ROOT / "scripts" / "run_experiment.py")
rx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rx)


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "load_env", lambda: {})
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    arts = []
    for aid in ("e1", "e2"):
        a = copy.deepcopy(ARTICLE)
        a["article_id"] = aid
        arts.append(a)
    src = tmp_path / "in.jsonl"
    src.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in arts), encoding="utf-8")
    fb = _ByArticle({"e1": _llm_doc(), "e2": _llm_doc(items=[_item(fit_raw="오버핏")])})  # e2: V3 → 검토 대기
    client.register_backend("openai_api", fb)
    return src, fb


def test_vtag():
    assert rx.vtag("extract-v4") == "v4" and rx.vtag("extract-v3") == "v3"


def test_reasoning_model_run_copies_responses_and_reports_tokens(monkeypatch, tmp_path):
    """추론 모델(추론 강도 있음)도 원 응답 사본이 남고, 실행 보고서가 OpenAI usage 키로 토큰을 센다."""
    src, _ = _setup(monkeypatch, tmp_path)

    class _OpenAIStyle(_ByArticle):
        def run(self, prompt, schema, **kw):
            r = super().run(prompt, schema, **kw)
            r.usage = {"prompt_tokens": 1200, "completion_tokens": 300,
                       "completion_tokens_details": {"reasoning_tokens": 200}}
            return r

    client.register_backend("openai_api", _OpenAIStyle({"e1": _llm_doc()}))
    try:
        rx.main(["--prompt-version", "extract-v4", "--model", "gpt-5-mini", "--reasoning-effort", "medium",
                 "--ids", "e1", "--in", str(src), "--out-root", str(tmp_path / "exp")])
    finally:
        client.register_backend("openai_api", None)
    run = next((tmp_path / "exp").glob("v4_gpt-5-mini-medium_*"))
    assert (run / "llm_responses" / "v4_e1_try1.json").exists()
    rec = json.loads((run / "v4_EXPERIMENT.json").read_text(encoding="utf-8"))
    assert rec["condition"]["reasoning_effort"] == "medium" and rec["condition"]["temperature"] is None
    assert rec["results"]["summary"]["tokens_reasoning"] == 200
    assert "입력 1,200 · 출력 300" in (run / "v4_run_report.md").read_text(encoding="utf-8")


def test_cost_usd_and_pricing_file():
    pricing = {"models": {"m": [2.0, 0.5, 8.0]}}
    # 입력 1M 중 0.4M 캐시 적중 → 0.6·2 + 0.4·0.5 = 1.4, 출력 0.1M·8 = 0.8
    assert rx.cost_usd("m", 1_000_000, 400_000, 100_000, pricing) == 2.2
    assert rx.cost_usd("없는모델", 10, 0, 10, pricing) is None
    real = rx.load_pricing()
    assert real["checked_at"] and real["models"]["gpt-4o-mini"] == [0.15, 0.075, 0.60]
    assert not any(k.endswith("-pro") for k in real["models"])  # pro 계열은 실험 대상에서 뺐다(사용자 결정)
    assert rx._tokens([{"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 40}}, None]) == (100, 40, 7)


def test_experiment_records_and_prefix(monkeypatch, tmp_path):
    src, fb = _setup(monkeypatch, tmp_path)
    try:
        code = rx.main(["--prompt-version", "extract-v4", "--model", "m-test", "--ids", "e1,e2", "--in", str(src),
                        "--out-root", str(tmp_path / "exp"), "--note", "합성 테스트"])
    finally:
        client.register_backend("openai_api", None)
    assert code == 0
    runs = list((tmp_path / "exp").glob("v4_m-test_*"))
    assert len(runs) == 1
    run = runs[0]
    assert run.name.startswith("v4_m-test_dedupe_namefix_")            # P1·P2 기본 켬 (D-27 채택)
    # 결과 파일은 프롬프트 버전 접두어를 단다
    assert (run / "v4_e1.json").exists() and (run / "_review" / "v4_e2.json").exists()
    assert not (run / "e1.json").exists()
    for name in ("v4_EXPERIMENT.md", "v4_EXPERIMENT.json", "v4_prompt_template.md", "v4_prompt_diff_vs_v3.diff",
                 "v4_llm_schema.json", "v4_code.patch", "v4_run_report.md", "v4_run.log"):
        assert (run / name).exists(), name
    assert (run / "llm_responses" / "v4_e1_try1.json").exists()
    rec = json.loads((run / "v4_EXPERIMENT.json").read_text(encoding="utf-8"))
    assert rec["prompt"]["prompt_version"] == "extract-v4" and rec["prompt"]["base"] == "extract-v3"
    assert rec["condition"]["model"] == "m-test" and rec["condition"]["ids"] == ["e1", "e2"]
    assert rec["condition"]["postprocess"]["dedupe"] is True and rec["condition"]["postprocess"]["name_fix"] is True
    assert rec["code"]["git_head"] and "scripts/run_experiment.py" in rec["code"]["file_sha256"]
    st = {a["article_id"]: a["status"] for a in rec["results"]["articles"]}
    assert st == {"e1": "passed", "e2": "review"}
    assert rec["results"]["summary"]["tokens_in"] > 0
    assert "합성 테스트" in (tmp_path / "exp" / "INDEX.md").read_text(encoding="utf-8")
    # 같은 조건을 다시 돌리면 캐시를 쓴다(새 호출 없음)
    n_calls = len(fb.calls)
    client.register_backend("openai_api", fb)
    try:
        rx.main(["--prompt-version", "extract-v4", "--model", "m-test", "--ids", "e1", "--in", str(src),
                 "--out-root", str(tmp_path / "exp2"), "--no-dedupe", "--no-name-fix"])
    finally:
        client.register_backend("openai_api", None)
    assert len(fb.calls) == n_calls
    off = next((tmp_path / "exp2").glob("v4_m-test_*"))
    assert "_dedupe" not in off.name and "_namefix" not in off.name     # 끄면 폴더 이름에 표시가 없다
