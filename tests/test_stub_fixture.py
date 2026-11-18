"""stub 그래프(합성 fixture): 요청 파일 · fixture 데이터 · 적재 안전장치는 Neo4j 없이, 실제 적재·질의는 neo4j 마커로 확인한다."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from fashion_planner_trend.api.server import CONTRACT_VERSION, _validate_request
from fashion_planner_trend.graph import fixture, loader
from fashion_planner_trend.schemas.article_extraction import ArticleExtraction
from fashion_planner_trend.schemas.trend_analysis_result import evidence_id

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "examples" / "stub"


def _json(name: str):
    return json.loads((STUB / name).read_text(encoding="utf-8"))


def test_stub_request_file_is_the_fixture_envelope_and_validates():
    req = _json("trend-request.json")
    assert req == fixture.stub_request() and req["contract_version"] == CONTRACT_VERSION
    request_id, query = _validate_request(req)
    assert request_id == query.request_id == fixture.STUB_REQUEST_ID


def test_stub_label_validates_and_links_example_images():
    ext = ArticleExtraction.model_validate(fixture.stub_label()["extraction"])
    assert [s.name for s in ext.styles] == ["발레코어"]
    assert loader.style_example_images(ext.styles, fixture.synthetic_crawl(fixture.STUB_ARTICLE_ID)) == [
        [("https://img.example.invalid/0.jpg", "item_link"), ("https://img.example.invalid/1.jpg", "article")]]


def test_summary_file_evidence_ids_are_the_stable_ids_of_the_fixture_quotes():
    summary = _json("trend-response-summary.json")
    assert summary["request_id"] == fixture.STUB_REQUEST_ID and summary["status"] == summary["result"]["status"] == "ok"
    result = summary["result"]
    assert result["trends"] and result["evidence_ids"]
    style = fixture.stub_label()["extraction"]["styles"][0]
    quotes = (style["evidence"] + style["description"][0]["evidence"] + style["focus_attributes"][0]["evidence"]
              + style["items"][0]["evidence"])  # 상의 요청이라 블라우스 Item만 남는다
    assert set(result["evidence_ids"]) == {evidence_id(fixture.STUB_ARTICLE_ID, e["block_idx"], e["quote"]) for e in quotes}
    assert all(fixture.EVIDENCE_ID_RE.match(x) for x in result["evidence_ids"])


class _Result:
    def __init__(self, records):
        self.records = records


class FakeDriver:
    def __init__(self, nodes: int):
        self.nodes, self.queries = nodes, []

    def execute_query(self, cypher, **params):
        self.queries.append(cypher)
        if cypher.startswith("MATCH (n) RETURN count(n)"):
            return _Result([{"c": self.nodes}])
        if cypher.startswith("MATCH (n) DETACH DELETE n"):
            self.nodes = 0
        return _Result([])

    def verify_connectivity(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_seed_refuses_non_empty_graph_without_allow_wipe(monkeypatch):
    d = FakeDriver(5)
    monkeypatch.setattr(fixture, "load_sources", lambda *a, **k: pytest.fail("must not load"))
    with pytest.raises(fixture.GraphNotEmpty) as exc:
        fixture.seed_stub_graph(d)
    assert exc.value.count == 5 and not any("DELETE" in q for q in d.queries)


@pytest.mark.parametrize("nodes", [0, 5])
def test_seed_wipes_only_when_allowed_then_loads_test_graph_plus_stub_article(monkeypatch, nodes):
    d = FakeDriver(nodes)
    seen = {}

    def fake_load(driver, root, products):
        seen["files"] = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())
        seen["products"] = [p.node.id for p in products]
        seen["deleted"] = any("DETACH DELETE" in q for q in driver.queries)
        return set(), {}

    monkeypatch.setattr(fixture, "load_sources", fake_load)
    monkeypatch.setattr(loader, "graph_counts", lambda driver: {})
    report = fixture.seed_stub_graph(d, allow_wipe=True)
    assert report["wiped_nodes"] == nodes and seen["deleted"] is bool(nodes)
    assert seen["files"] == ["crawl.jsonl", "ext/900001.json", "ext/900002.json", "labels/900001.json", "labels/900003.json"]
    assert {"otpensource:a", "otpensource:p1", "otpensource:p2"} <= set(seen["products"])


@pytest.mark.parametrize("uri,managed", [("neo4j+s://abcd1234.databases.neo4j.io", True), ("bolt://neo4j-stub:7687", False),
                                         ("bolt://127.0.0.1:7688", False), ("neo4j://neo4j.io.example:7687", False)])
def test_managed_cloud_detection(uri, managed):
    assert fixture.is_managed_cloud(uri) is managed


def _script():
    spec = importlib.util.spec_from_file_location("seed_fixture_graph", ROOT / "scripts" / "seed_fixture_graph.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def neo4j_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("NEO4J_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("NEO4J_PASSWORD", "synthetic-password-never-printed")
    return monkeypatch


@pytest.mark.parametrize("uri", [None, "neo4j+s://abcd1234.databases.neo4j.io"])
def test_script_refuses_before_connecting_without_uri_or_on_aura(neo4j_env, capsys, uri):
    """NEO4J_URI는 프로세스 환경에서만 읽는다: 레포 .env에 값이 있어도 없으면 거부한다."""
    if uri:
        neo4j_env.setenv("NEO4J_URI", uri)
    script = _script()
    neo4j_env.setattr(script, "neo4j_driver", lambda *a, **k: pytest.fail("must not connect"))
    assert script.main([]) == 2
    out = capsys.readouterr()
    assert "거부" in out.err and "synthetic-password-never-printed" not in out.out + out.err


def test_script_refuses_non_empty_graph_without_printing_the_password(neo4j_env, capsys):
    neo4j_env.setenv("NEO4J_URI", "bolt://127.0.0.1:7699")
    script = _script()
    neo4j_env.setattr(script, "neo4j_driver", lambda settings: FakeDriver(3))
    assert script.main([]) == 2
    out = capsys.readouterr()
    assert "--allow-wipe" in out.err and "bolt://127.0.0.1:7699" in out.err
    assert "synthetic-password-never-printed" not in out.out + out.err


@pytest.mark.neo4j
def test_seeded_stub_graph_answers_the_stub_query(neo4j_test_driver):
    d = neo4j_test_driver
    report = fixture.seed_stub_graph(d, allow_wipe=True)
    assert report["articles"][fixture.STUB_ARTICLE_ID]["from"] == "label"
    result = fixture.run_stub_query(d)
    assert fixture.summarize_result(result) == _json("trend-response-summary.json")["result"]
    with pytest.raises(fixture.GraphNotEmpty):
        fixture.seed_stub_graph(d)
