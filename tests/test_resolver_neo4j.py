"""Resolver Tier1~4 (Neo4j 테스트 인스턴스 7688, 합성 데이터, 가짜 LLM·임베딩). 그래프를 비운다."""
import json
from types import SimpleNamespace

import pytest

from conftest import synthetic_crawl, synthetic_label, synthetic_products
from trend_agent.graph import loader
from trend_agent.graph.search import TrendSearcher
from trend_agent.graph.terms import rebuild_terms
from trend_agent.resolver import ResolveConfig, TrendResolver
from trend_agent.resolver import term_map as tm
from trend_agent.retrieval.weights import ResolverConfig, Tier3Config, load_weights
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.trend_analysis_result import TrendAnalysisResult
from trend_agent.schemas.trend_query import SeasonRequest

pytestmark = pytest.mark.neo4j

W = load_weights()


@pytest.fixture(scope="module")
def driver(neo4j_test_driver, tmp_path_factory):
    d = neo4j_test_driver
    d.execute_query("MATCH (n) DETACH DELETE n")
    tmp = tmp_path_factory.mktemp("resolver")
    (tmp / "labels").mkdir()
    (tmp / "ext").mkdir()
    other = synthetic_label("900003", aliases=())
    other["extraction"]["styles"][0]["name"] = "다른 트렌드"
    (tmp / "labels" / "900001.json").write_text(json.dumps(synthetic_label(), ensure_ascii=False))
    (tmp / "labels" / "900003.json").write_text(json.dumps(other, ensure_ascii=False))
    (tmp / "ext" / "900002.json").write_text(json.dumps(synthetic_label("900002", status="auto", aliases=(), seasons=()), ensure_ascii=False))
    (tmp / "crawl.jsonl").write_text("\n".join(json.dumps(synthetic_crawl(a), ensure_ascii=False) for a in ("900001", "900002", "900003")))
    loader.ensure_schema(d)
    loader.seed_vocabulary(d)
    loader.load_products(d, synthetic_products())
    loader.load_articles(d, tmp / "labels", tmp / "ext", tmp / "crawl.jsonl")
    rebuild_terms(d, [tm.TermMapRow(row="alias", term="Quiet Luxury", kind="translation", origin="manual",
                                    article_id="900001", style_norm="테스트럭셔리")])
    return d


def R(driver, weights=W, **cfg):
    kw = {k: cfg.pop(k) for k in ("llm", "embedder", "term_rows", "term_map_path", "mentions", "crawl") if k in cfg}
    kw.setdefault("term_rows", [])
    return TrendResolver(TrendSearcher(driver, weights=weights), ResolveConfig(**cfg), llm_settings=("fake", "fake-m"), **kw)


def season(s="2027SS"):
    return SeasonRequest(year=int(s[:4]), season=V.Season(s[4:]), origin="user_requested")


def codes(res):
    return [f"T{s.tier}:{s.outcome}:{s.code}" for s in res.steps]


# ---------- Tier1 ----------
def test_tier1_name_report_policy(driver):
    out = R(driver, season=season()).run(["테스트 럭셔리"], "r1")
    r = out.resolutions[0]
    assert r.status == "resolved" and r.tier == 1 and r.match_mode == "graph" and r.qualification == "article_evidence"
    assert codes(r) == ["T1:hit:name_exact", "T1:hit:season_reported"]
    assert sorted(r.style_ids) == ["900001-s0", "900002-s0"] and out.status == "ok"
    st = {t.style.id: t.style.season_status for t in out.trends}
    assert st == {"900001-s0": "match", "900002-s0": "unknown"}
    assert r.top_products and r.top_products[0].rank == 1 and r.top_products[0].item_id.startswith("90000")
    assert all(e.evidence_id.startswith("trend:") for e in out.evidence)
    assert out.applied_settings.season_policy == "report" and out.applied_settings.weights_sha == W.sha


def test_tier1_alias_and_term(driver):
    r = R(driver).run(["콰이어트 럭셔리"], "r").resolutions[0]
    assert r.tier == 1 and r.style_ids == ["900001-s0"]
    t = R(driver).run(["Quiet Luxury"], "r").resolutions[0]
    assert t.tier == 1 and t.style_ids == ["900001-s0"] and "Term ALIAS_OF" in t.steps[0].detail


def test_tier1_strict_season_stops(driver):
    out = R(driver, season=season("2026FW"), season_policy="strict").run(["테스트 럭셔리"], "r")
    r = out.resolutions[0]
    assert r.status == "failed" and codes(r) == ["T1:miss:season_unknown_held"] and out.status == "no_results"
    only = R(driver, season=season("2026FW"), season_policy="strict").run(["다른 트렌드"], "r").resolutions[0]
    assert codes(only) == ["T1:miss:season_mismatch"]


def test_name_not_found_suggests_but_does_not_hit(driver):
    r = R(driver, use_llm=False).run(["럭셔리"], "r").resolutions[0]
    assert r.steps[0].code == "name_not_found" and "부분 일치 제안" in (r.steps[0].detail or "")
    assert r.status == "failed"


# ---------- Tier2 ----------
def test_tier2_virtual_profile(driver):
    out = R(driver).run(["베이지 셔츠"], "r")
    r = out.resolutions[0]
    assert r.tier == 2 and r.match_mode == "attribute" and r.qualification == "vocabulary_rule"
    assert codes(r) == ["T1:miss:name_not_found", "T2:hit:vocab_full"]
    t = out.trends[0]
    assert t.style.virtual and t.style.id == "virtual:2:베이지셔츠" and t.style.name_origin == "query"
    assert [i.id for i in t.items] == ["virtual:2:베이지셔츠-i0"] and t.items[0].virtual
    assert r.top_products[0].product_id == "otpensource:a"  # 베이지 + 셔츠 하분류


# ---------- Tier3 ----------
class FakeEmb:
    def __init__(self, styles, terms=()):
        self.styles, self.terms = list(styles), list(terms)

    def embed_texts(self, texts):
        return [[0.0]]

    def search(self, label, vec, k=10):
        return self.styles if label == "Style" else self.terms


def w_tier3(tau=0.6, delta=0.05):
    return W.model_copy(update={"resolver": ResolverConfig(tier3=Tier3Config(tau=tau, delta=delta, model="fake"))})


def test_tier3_disabled_by_default(driver):
    r = R(driver, use_llm=False).run(["럭셔리 무드"], "r").resolutions[0]
    assert "T3:skipped:tier3_disabled" in codes(r)


def test_tier3_semantic_hit_groups_and_delta(driver):
    emb = FakeEmb([("900001-s0", 0.82), ("900002-s0", 0.79), ("900003-s0", 0.70)])
    res = R(driver, w_tier3(), embedder=emb, use_llm=False)
    cands = res.tier3_candidates("럭셔리 무드")
    assert [(c.group_norm, c.score, c.style_ids) for c in cands] == [
        ("테스트럭셔리", 0.82, ["900001-s0", "900002-s0"]), ("다른", 0.70, ["900003-s0"])]
    r = res.run(["럭셔리 무드"], "r").resolutions[0]
    assert r.tier == 3 and r.match_mode == "semantic" and r.similarity == 0.82 and sorted(r.style_ids) == ["900001-s0", "900002-s0"]
    amb = R(driver, w_tier3(delta=0.2), embedder=emb, use_llm=False).run(["럭셔리 무드"], "r").resolutions[0]
    assert "T3:miss:ambiguous_delta" in codes(amb)
    low = R(driver, w_tier3(tau=0.9), embedder=emb, use_llm=False).run(["럭셔리 무드"], "r").resolutions[0]
    assert "T3:miss:below_tau" in codes(low)
    hidden = R(driver, w_tier3(), embedder=emb, use_llm=False, hide_styles=["900001-s0"]).tier3_candidates("x")
    assert [c.group_norm for c in hidden] == ["다른"]  # name_norm 그룹째 숨김
    broken = R(driver, w_tier3(), embedder=SimpleNamespace(), use_llm=False).run(["럭셔리 무드"], "r").resolutions[0]
    assert "T3:error:tier3_disabled" in codes(broken)


# ---------- Tier4 ----------
class FakeLLM:
    class LLMOutputError(Exception):
        pass

    def __init__(self, outputs):
        self.outputs, self.prompts = list(outputs), []

    def call(self, prompt, schema, *, purpose, prompt_version):
        assert purpose == "tier4_profile" and prompt_version == "tier4.v1" and schema["additionalProperties"] is False
        self.prompts.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(data=out, meta={"backend": "fake", "model": "fake-m"})


def profile(**over):
    d = {"term": "드뮤어", "recognized": True, "summary": "단정한 차림",
         "focus": {"color": "core", "material": "supporting", "pattern": None, "detail": None, "fit": None, "category": None},
         "items": [{"category_major": "상의", "category_minor": "셔츠", "level": "core",
                    "colors": [{"value": "베이지", "value_level": "primary"}, {"value": "화이트", "value_level": "secondary"}],
                    "materials": [{"value": "면", "value_level": "primary"}], "patterns": [], "details": [], "fit": None}],
         "evidence": [], "no_evidence": True}
    d.update(over)
    return d


def test_tier4_retry_cache_and_numbers_from_code(driver, tmp_path):
    path = tmp_path / "term_map.jsonl"
    bad = profile(focus={a: "core" for a in ("color", "material", "pattern", "detail", "fit", "category")})
    llm = FakeLLM([bad, profile()])
    out = R(driver, llm=llm, term_map_path=path).run(["드뮤어"], "r")
    r = out.resolutions[0]
    assert r.tier == 4 and r.match_mode == "llm_inferred" and r.qualification == "agent_proposal"
    assert codes(r)[-1] == "T4:hit:llm_profile" and len(llm.prompts) == 2 and "검증 오류" in llm.prompts[1]
    assert r.llm.cache == "miss" and r.llm.evidence == "none" and r.llm.backend == "fake"
    item = out.trends[0].items[0]
    assert item.id == "virtual:4:드뮤어-i0" and item.value_weights["color"] == {"베이지": 1.0, "화이트": 0.5}
    assert item.similar_products and item.similar_products[0].score_breakdown.value_weighted
    assert any("agent_proposal" in x for x in out.limitations)
    rows = tm.read_term_map(path)
    assert len(rows) == 1 and rows[0].row == "profile" and rows[0].model == "fake-m"
    # 2회차: 캐시 적중, LLM 호출 없음
    llm2 = FakeLLM([])
    r2 = R(driver, llm=llm2, term_rows=tm.read_term_map(path), term_map_path=path).run(["드뮤어"], "r").resolutions[0]
    assert codes(r2)[-1] == "T4:hit:llm_cache_hit" and r2.llm.cache == "hit" and llm2.prompts == []
    assert [p.product_id for p in r2.top_products] == [p.product_id for p in r.top_products]


def test_tier4_unrecognized_invalid_unavailable(driver, tmp_path):
    unk = R(driver, llm=FakeLLM([{"term": "x", "recognized": False, "summary": None, "focus": {}, "items": [], "evidence": [],
                                  "no_evidence": True}]), term_map_path=tmp_path / "a.jsonl").run(["모르는말"], "r")
    assert codes(unk.resolutions[0])[-1] == "T4:miss:llm_unrecognized" and unk.status == "no_results"
    bad = profile(items=[])
    inv = R(driver, llm=FakeLLM([bad, bad]), term_map_path=tmp_path / "b.jsonl").run(["모르는말"], "r").resolutions[0]
    assert codes(inv)[-1] == "T4:error:llm_invalid_output"
    off = R(driver, llm=FakeLLM([RuntimeError("codex 없음")]), term_map_path=tmp_path / "c.jsonl").run(["모르는말"], "r").resolutions[0]
    assert codes(off)[-1] == "T4:error:llm_unavailable" and "codex 없음" in off.steps[-1].detail
    nollm = R(driver, use_llm=False).run(["모르는말"], "r").resolutions[0]
    assert codes(nollm)[-1] == "T4:skipped:llm_unavailable"


def test_tier4_evidence_from_mentions(driver, tmp_path):
    crawl = {"900001": synthetic_crawl("900001")}
    crawl["900001"]["blocks"][3]["text"] = "요즘 드뮤어한 베이지 셔츠가 보인다."
    mentions = {"드뮤어": {"term": "드뮤어", "evidence": [{"article_id": "900001", "block_idx": 3, "role": "mention"}]}}
    p = profile(evidence=[{"ref": "900001:3", "quote": "드뮤어한 베이지 셔츠"}], no_evidence=False)
    out = R(driver, llm=FakeLLM([p]), mentions=mentions, crawl=crawl, term_map_path=tmp_path / "t.jsonl").run(["드뮤어"], "r")
    r = out.resolutions[0]
    assert r.llm.evidence == "provided" and out.evidence[0].evidence_id.startswith("trend:900001:3:")
    assert out.trends[0].style.description[0].evidence_ids == [out.evidence[0].evidence_id]
    none = R(driver, llm=FakeLLM([profile()]), mentions=mentions, crawl=crawl, tier4_context="none",
             term_map_path=tmp_path / "u.jsonl").run(["드뮤어"], "r")
    assert none.resolutions[0].llm.evidence == "none" and none.applied_settings.tier4_context == "none"
    assert none.resolutions[0].llm.context_sha != r.llm.context_sha


# ---------- 평가 실행 · 상태 · 진입점 ----------
def test_hide_and_force_tier(driver, tmp_path):
    hid = R(driver, hide_styles=["900001-s0"], use_llm=False).run(["테스트 럭셔리"], "r")
    r = hid.resolutions[0]
    assert codes(r)[:2] == ["T1:skipped:hidden_by_eval", "T1:miss:name_not_found"] and r.status == "failed"
    assert hid.applied_settings.hidden_style_groups == ["테스트럭셔리"]
    f = R(driver, force_tier=4, llm=FakeLLM([profile(term="테스트 럭셔리")]), term_map_path=tmp_path / "f.jsonl").run(["테스트 럭셔리"], "r")
    assert codes(f.resolutions[0])[:3] == ["T1:skipped:forced_tier", "T2:skipped:forced_tier", "T3:skipped:forced_tier"]
    assert f.resolutions[0].tier == 4 and f.applied_settings.force_tier == 4


def test_status_partial_and_articles_and_determinism(driver):
    out = R(driver, use_llm=False).run(["테스트 럭셔리", "없는말xyz", "테스트럭셔리"], "r")  # 같은 norm은 한 번
    assert out.status == "partial" and [r.term for r in out.resolutions] == ["테스트 럭셔리", "없는말xyz"]
    arts = R(driver).run_articles(["900003", "999999"], "r")
    assert [r.term for r in arts.resolutions] == ["다른 트렌드"] and any("999999" in x for x in arts.limitations)
    a = R(driver, season=season()).run(["테스트 럭셔리", "베이지 셔츠"], "x").model_dump()
    b = R(driver, season=season()).run(["테스트 럭셔리", "베이지 셔츠"], "x").model_dump()
    assert a == b
    TrendAnalysisResult.model_validate_json(json.dumps(a, ensure_ascii=False))  # 계약 왕복
