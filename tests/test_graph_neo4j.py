"""Neo4j 테스트 인스턴스(7688)에서: 적재 v1.1 · 속성 노드 출발 후보 · 사유 구분 · 안정 evidence_id.

합성 데이터만 쓴다. **그래프를 비운다** — conftest가 7687을 거부한다.
"""
import json
from types import SimpleNamespace

import pytest

from conftest import synthetic_crawl, synthetic_label, synthetic_products
from trend_agent.graph import loader
from trend_agent.graph.search import ITEMS_CYPHER, TrendSearcher, _record
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import ItemNode, ItemRecord
from trend_agent.schemas.trend_query import Requested, SeasonRequest, TermRef, TrendQuery, TrendTerms

pytestmark = pytest.mark.neo4j

def _load(d, tmp):
    loader.ensure_schema(d)
    loader.seed_vocabulary(d)
    seen: set = set()
    loader.load_products(d, synthetic_products(), seen=seen)
    stats = loader.load_articles(d, tmp / "labels", tmp / "ext", tmp / "crawl.jsonl", prefer="labels")
    return seen, stats


@pytest.fixture(scope="module")
def g(neo4j_test_driver, tmp_path_factory):
    d = neo4j_test_driver
    d.execute_query("MATCH (n) DETACH DELETE n")
    tmp = tmp_path_factory.mktemp("graph")
    (tmp / "labels").mkdir()
    (tmp / "ext").mkdir()
    (tmp / "labels" / "900001.json").write_text(json.dumps(synthetic_label(), ensure_ascii=False))
    (tmp / "ext" / "900001.json").write_text(json.dumps(synthetic_label(status="auto"), ensure_ascii=False))  # 라벨이 이긴다
    (tmp / "ext" / "900002.json").write_text(json.dumps(
        synthetic_label("900002", status="auto", color_raw="차콜 그레이", aliases=(), seasons=()), ensure_ascii=False))
    (tmp / "crawl.jsonl").write_text("\n".join(json.dumps(synthetic_crawl(a), ensure_ascii=False) for a in ("900001", "900002")))
    seen, stats = _load(d, tmp)
    return SimpleNamespace(d=d, tmp=tmp, seen=seen, stats=stats)


def _q(d, cypher, **kw):
    return [dict(r) for r in d.execute_query(cypher, **kw).records]


def test_articles_sources_and_props(g):
    assert g.stats["900001"]["from"] == "label" and g.stats["900002"]["from"] == "extraction"
    arts = {r["id"]: r for r in _q(g.d, "MATCH (a:Article) RETURN a.id AS id, a.source AS source, a.labeler AS labeler")}
    assert arts["900001"]["source"] == "label" and arts["900002"]["labeler"] == "llm:openai_api/test-model"


def test_item_image_urls_from_crawl_map(g):
    r = _q(g.d, "MATCH (i:Item {id: '900001-s0-i0'}) RETURN i.image_urls AS u, i.image_basis AS b")[0]
    assert r == {"u": ["https://img.example.invalid/0.jpg"], "b": ["adjacent"]}
    assert g.stats["900001"]["image_links"] == 2 and g.stats["900001"]["image_links_unmapped"] == 1


def test_auto_extraction_approx_edge(g):
    r = _q(g.d, "MATCH (:Item {id: '900002-s0-i0'})-[r:HAS_COLOR]->(c) RETURN c.id AS c, r.approx AS a, r.raw AS raw")
    assert r == [{"c": "그레이", "a": True, "raw": "차콜 그레이"}] and g.stats["900002"]["approx_marked"] == 1
    lab = _q(g.d, "MATCH (:Item {id: '900001-s0-i0'})-[r:HAS_COLOR]->() RETURN r.approx AS a")
    assert lab == [{"a": False}]


def test_style_v11_props(g):
    r = _q(g.d, "MATCH (s:Style {id: '900001-s0'}) RETURN s.name_norm AS n, s.aliases_norm AS an, s.article_id AS aid")[0]
    assert r == {"n": "테스트럭셔리", "an": ["콰이어트럭셔리"], "aid": "900001"}


def test_fulltext_indexes_list_property(g):
    hits = _q(g.d, "CALL db.index.fulltext.queryNodes('style_names', '콰이어트') YIELD node RETURN node.id AS id")
    assert [h["id"] for h in hits] == ["900001-s0"]


def test_reload_is_idempotent(g):
    before = loader.graph_counts(g.d)
    _load(g.d, g.tmp)
    assert loader.graph_counts(g.d) == before


def _legacy_candidates(d, item):
    """v1.0 후보 쿼리 — Item 노드 출발. 새 쿼리와 집합이 같아야 한다."""
    rel = "HAS_COLOR|HAS_MATERIAL|HAS_PATTERN|HAS_DETAIL"
    major = item.category_major.value
    ids = set()
    if item.node.category_minor:
        ids |= {r["id"] for r in _q(d, "MATCH (p:Product {category_minor: $m})-[:IN_CATEGORY]->(:Category {id: $major}) RETURN p.id AS id",
                                    m=item.node.category_minor.value, major=major)}
    fit = item.usable_scalar("fit")
    if fit is not None:
        ids |= {r["id"] for r in _q(d, "MATCH (p:Product {fit: $f})-[:IN_CATEGORY]->(:Category {id: $major}) WHERE NOT 'fit' IN p.approx RETURN p.id AS id",
                                    f=fit.value, major=major)}
    ids |= {r["id"] for r in _q(
        d, f"MATCH (i:Item {{id: $iid}})-[ri:{rel}]->(a)<-[rp:{rel}]-(p:Product)-[:IN_CATEGORY]->(:Category {{id: $major}}) "
           "WHERE type(ri) = type(rp) AND NOT coalesce(ri.approx, false) AND NOT coalesce(rp.approx, false) RETURN DISTINCT p.id AS id",
        iid=item.node.id, major=major)}
    return ids


def test_attribute_origin_candidates_equal_legacy_and_work_for_virtual(g):
    s = TrendSearcher(g.d)
    items = [_record(ItemRecord, ItemNode, r) for r in g.d.execute_query(ITEMS_CYPHER, style_ids=["900001-s0", "900002-s0"]).records]
    assert len(items) == 4
    for it in items:
        legacy = _legacy_candidates(g.d, it)
        assert set(s.candidate_ids(it)) == legacy
        virtual = it.model_copy(update={"node": it.node.model_copy(update={"id": "virtual:4:x-i0"})})
        assert set(s.candidate_ids(virtual)) == legacy and legacy
    # 근사(차콜 그레이 → 그레이) 값은 후보에 쓰지 않는다: 900002 셔츠의 후보는 하분류·소재만
    approx_item = next(i for i in items if i.node.id == "900002-s0-i0")
    assert set(s.candidate_ids(approx_item)) == {"otpensource:a", "otpensource:b", "musinsa_snap:1"}


def test_fast_path_image_first_order(g):
    s = TrendSearcher(g.d)
    bare = ItemRecord(node=ItemNode(id="virtual:2:x-i0", name="셔츠", raw_text="", article_id="virtual", category_minor="셔츠"),
                      category_major="상의")
    assert s.candidate_ids(bare, limit_when_no_attributes=2) == ["musinsa_snap:1", "otpensource:a"]  # id 순 (m < o)
    # 이미지 우선: a(ok)·snap:1(ok) > b(broken) — 같은 집합이지만 limit 1이면 차이가 난다
    assert s.candidate_ids(bare, limit_when_no_attributes=1) == ["musinsa_snap:1"]
    assert s.candidate_ids(bare, limit_when_no_attributes=3, image_first=True) == ["musinsa_snap:1", "otpensource:a", "otpensource:b"]


def _query(season="2027SS", names=()):
    return TrendQuery(request_id="t", original_query="t",
                      trend_terms=TrendTerms(season=SeasonRequest(year=int(season[:4]), season=V.Season(season[4:]), origin="user_requested"),
                                             style_names=[TermRef(raw=n) for n in names]),
                      requested=Requested())


def test_search_reasons_and_stable_evidence_ids(g):
    s = TrendSearcher(g.d, top_k=3)
    ok = s.search(_query(names=["테스트 럭셔리"]))
    assert ok.status == "ok" and [t.style.id for t in ok.trends] == ["900001-s0"]
    assert any("시즌이 확인되지 않은 트렌드 1개" in x for x in ok.limitations)  # 900002 (시즌 없음)
    assert all(e.evidence_id.startswith("trend:900001:") for e in ok.evidence)
    again = s.search(_query(names=["테스트 럭셔리"]))
    assert [e.evidence_id for e in again.evidence] == [e.evidence_id for e in ok.evidence]
    top = ok.trends[0].items[0].similar_products[0]
    assert top.product_id == "otpensource:a" and top.has_image and top.image_status == "ok"
    miss = s.search(_query("2026FW", ["테스트 럭셔리"]))
    assert miss.status == "no_results" and any("시즌이 아닌 트렌드 1개" in x for x in miss.limitations)
    none = s.search(_query(names=["없는말"]))
    assert none.status == "no_results" and any("일치하는 트렌드가 그래프에 없습니다" in x for x in none.limitations)


def test_prune_products(g):
    keep = set(g.seen) - {"otpensource:e"}
    assert loader.prune_products(g.d, keep) == 1
    assert not _q(g.d, "MATCH (p:Product {id: 'otpensource:e'}) RETURN p.id")
    loader.load_products(g.d, [p for p in synthetic_products() if p.node.id == "otpensource:e"])
