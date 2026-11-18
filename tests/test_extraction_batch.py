"""일괄 추출(재개·검토 대기열·연속 실패 중단·보고서) — 합성 기사, 실제 LLM 호출 없음."""
from __future__ import annotations

import copy
import json

import pytest

from trend_agent.ingestion.extraction.batch import BatchConfig, run_batch
from trend_agent.llm import LLMBackendError, client
from trend_agent.llm.backends import BackendReply

from test_extraction import ARTICLE, _item, _llm_doc


def _article(aid: str) -> dict:
    a = copy.deepcopy(ARTICLE)
    a["article_id"] = aid
    return a


class _ByArticle:
    """프롬프트의 article_id 줄로 응답을 고른다. 값이 Exception이면 던진다."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def run(self, prompt, schema, *, name, model, reasoning_effort, timeout_s, **kw):
        aid = next(line.split(": ", 1)[1] for line in prompt.splitlines() if line.startswith("- article_id: "))
        self.calls.append(aid)
        v = self.table[aid]
        if isinstance(v, Exception):
            raise v
        return BackendReply(data=copy.deepcopy(v), raw_text="{}", latency_s=2.0, thread_id=f"th-{aid}",
                            usage={"input_tokens": 1000, "output_tokens": 100})


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "load_env", lambda: {})
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))
    yield tmp_path
    client.register_backend("openai_api", None)


def _cfg(tmp_path, **kw):
    return BatchConfig(input_path=tmp_path / "unused.jsonl", out_dir=tmp_path / "article-extraction", model="m-1",
                       backend="openai_api", prompt_version="article-extraction", **kw)


def test_batch_pass_review_resume_and_report(env):
    good, bad = _llm_doc(), _llm_doc(items=[_item(fit_raw="오버핏")])  # bad: V3 → 재시도 후에도 실패
    fb = _ByArticle({"a1": good, "a2": bad, "a3": {"styles": []}})
    client.register_backend("openai_api", fb)
    arts = [_article("a1"), _article("a2"), _article("a3")]
    s = run_batch(_cfg(env), articles=arts)
    out = env / "article-extraction"
    assert (s.passed, s.review, s.failed, s.skipped) == (2, 1, 0, 0)
    assert (out / "a1.json").exists() and (out / "a3.json").exists() and (out / "_review" / "a2.json").exists()
    assert sorted(fb.calls) == ["a1", "a2", "a2", "a3"]  # a2만 1회 재시도
    q = [json.loads(x) for x in (env / "review_queue.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["article_id"] for r in q] == ["a2"] and q[0]["n_errors"] >= 1
    doc = json.loads((out / "a1.json").read_text(encoding="utf-8"))
    assert doc["label_meta"]["status"] == "auto" and doc["label_meta"]["validation"]["passed"] is True
    report = (out / "run_report.md").read_text(encoding="utf-8")
    assert "통과 **2**" in report and "검토 대기 **1**" in report and "| V3 | 2 | 1 |" in report
    assert "음성(Style 0) 기사 1" in report

    # 재개: 이미 있는 것은 건너뛴다 (호출 없음)
    fb.calls.clear()
    s2 = run_batch(_cfg(env), articles=arts)
    assert s2.skipped == 3 and fb.calls == []
    # --retry-review(캐시 사용): 검토 대상만 다시 검증하고 LLM은 부르지 않는다(검증기만 바뀐 경우)
    fb.table["a2"] = good
    s3 = run_batch(_cfg(env, retry_review=True), articles=arts)
    assert fb.calls == [] and s3.review == 1
    # --retry-review --no-cache: 새 응답을 받고, 통과하면 _review에서 옮긴다
    s3 = run_batch(_cfg(env, retry_review=True, use_cache=False), articles=arts)
    assert fb.calls == ["a2"] and s3.passed == 1
    assert (out / "a2.json").exists() and not (out / "_review" / "a2.json").exists()


def test_batch_stops_after_consecutive_backend_errors(env):
    err = LLMBackendError("openai_api HTTP 429: rate limit reached")
    fb = _ByArticle({f"b{i}": err for i in range(6)})
    client.register_backend("openai_api", fb)
    s = run_batch(_cfg(env, concurrency=1, max_consecutive_failures=3), articles=[_article(f"b{i}") for i in range(6)])
    assert s.stopped_early and s.failed == 3 and len(fb.calls) == 3
    out = env / "article-extraction"
    assert not list(out.glob("b*.json"))  # 실패 기사는 파일을 쓰지 않는다(다음 실행에서 다시)
    log = [json.loads(x) for x in (out / "run.log").read_text(encoding="utf-8").splitlines()]
    assert {r["status"] for r in log} == {"failed"}


def test_batch_rejects_high_concurrency(env):
    with pytest.raises(SystemExit):
        run_batch(_cfg(env, concurrency=3), articles=[])


def test_batch_ids_order_and_unknown(env):
    client.register_backend("openai_api", _ByArticle({"c1": _llm_doc(), "c2": _llm_doc()}))
    arts = [_article("c1"), _article("c2")]
    s = run_batch(_cfg(env, ids=["c2"]), articles=arts)
    assert s.passed == 1 and (env / "article-extraction" / "c2.json").exists() and not (env / "article-extraction" / "c1.json").exists()
    with pytest.raises(SystemExit):
        run_batch(_cfg(env, ids=["zz"]), articles=arts)


def test_reprocess_uses_cache_and_rewrites(env):
    good = _llm_doc()
    fb = _ByArticle({"r1": good})
    client.register_backend("openai_api", fb)
    arts = [_article("r1")]
    run_batch(_cfg(env), articles=arts)
    first = json.loads((env / "article-extraction" / "r1.json").read_text(encoding="utf-8"))
    s = run_batch(_cfg(env, reprocess=True), articles=arts)
    assert fb.calls == ["r1"] and s.passed == 1 and s.skipped == 0  # 두 번째는 캐시 — LLM 호출 없음
    again = json.loads((env / "article-extraction" / "r1.json").read_text(encoding="utf-8"))
    assert again["extraction"] == first["extraction"] and again["label_meta"]["llm"]["cache_hit"] == [True]
