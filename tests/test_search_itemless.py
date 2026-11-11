"""D-42: Item이 0개인 Style은 그래프에 있어도 검색 결과 후보에서 빠지고 limitations로 알린다 (Neo4j 없이, 합성 Style)."""
from trend_agent.graph.search import TrendSearcher
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.trend_query import Requested, SeasonRequest, TermRef, TrendQuery, TrendTerms


class FakeSearcher(TrendSearcher):
    def __init__(self, styles):
        super().__init__(driver=None)
        self._styles = styles

    def styles(self):
        return self._styles

    def style_items(self, style_ids):
        return {}


def _style(sid, name, has_items):
    return {"id": sid, "name": name, "name_origin": "article", "seasons": ["2026FW"], "definition": "합성 정의",
            "_has_items": has_items}


def _query(names=()):
    return TrendQuery(request_id="t", original_query="t",
                      trend_terms=TrendTerms(season=SeasonRequest(year=2026, season=V.Season("FW"), origin="user_requested"),
                                             style_names=[TermRef(raw=n) for n in names]),
                      requested=Requested())


def test_itemless_style_excluded_with_limitation():
    s = FakeSearcher([_style("900001-s0", "테스트 핑크", False), _style("900002-s0", "테스트 핑크", False)])
    res = s.search(_query(["테스트 핑크"]))
    assert res.status == "no_results" and not res.trends
    assert any("연결된 옷(Item)이 없어 상품 검색에서 뺀 트렌드 2개" in x for x in res.limitations)


def test_season_only_query_all_itemless_does_not_claim_no_trend():
    s = FakeSearcher([_style("900001-s0", "테스트 핑크", False)])
    res = s.search(_query())
    assert res.status == "no_results"
    assert not any("유행한 트렌드가 그래프에 없습니다" in x for x in res.limitations)
    assert any("뺀 트렌드 1개: 테스트 핑크" in x for x in res.limitations)


def test_styles_with_items_are_kept():
    s = FakeSearcher([_style("900001-s0", "테스트 핑크", True), _style("900002-s0", "테스트 핑크", False)])
    res = s.search(_query(["테스트 핑크"]))
    assert any("뺀 트렌드 1개" in x for x in res.limitations)
