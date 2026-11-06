from datetime import date

import pytest
from pydantic import ValidationError

from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.article_extraction import (
    ArticleExtraction,
    Evidence,
    ExtractedDetail,
    ExtractedItem,
    ExtractedStyle,
    FocusAttribute,
)
from trend_agent.schemas.graph_schema import (
    compute_style_profile,
    flatten,
    item_record_from_extraction,
    style_node_from_extraction,
)
from trend_agent.schemas.trend_analysis_result import TrendAnalysisResult
from trend_agent.schemas.trend_query import (
    CategoryRef,
    RequestedAttribute,
    SeasonRequest,
    TrendQuery,
    style_matches_season,
    TrendTerms,
    next_fashion_season,
)

EV = [Evidence(block_idx=1, quote="q")]


def test_vocabulary_sizes():
    assert len(V.Color) == 21
    assert len(V.CategoryMajor) == 4 and len(V.CategoryMinor) == 21
    assert len(V.Material) == 26 and len(V.Pattern) == 19
    assert (len(V.DetailName), len(V.Neckline), len(V.Collar)) == (40, 12, 10)
    assert len(V.BodyLength) == 9 and len(V.SleeveLength) == 5
    assert len(V.Fit) == 7 and len(V.Shape) == 10
    assert set(V.MINOR_TO_MAJOR) == set(V.CategoryMinor)


def test_detail_node_id_distinguishes_type():
    assert V.detail_node_id(V.DetailType.DETAIL, "후드") != V.detail_node_id(V.DetailType.NECKLINE, "후드")


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 5, 10), (2026, V.Season.FW)),
        (date(2026, 11, 6), (2027, V.Season.SS)),
        (date(2026, 12, 31), (2027, V.Season.SS)),
        (date(2027, 1, 15), (2027, V.Season.SS)),
        (date(2027, 3, 1), (2027, V.Season.FW)),
        (date(2026, 8, 31), (2026, V.Season.FW)),
        (date(2026, 9, 1), (2027, V.Season.SS)),
    ],
)
def test_next_fashion_season(today, expected):
    assert next_fashion_season(today) == expected
    s = SeasonRequest.default_next(today)
    assert (s.year, s.season, s.origin, s.raw) == (*expected, "default_next", None)


def test_category_ref_derives_major_from_minor():
    assert CategoryRef(raw="셔츠", minor=V.CategoryMinor.SHIRT).major == V.CategoryMajor.UPPER
    assert CategoryRef(raw="상의", major=V.CategoryMajor.UPPER).minor is None


def test_requested_attribute_value_must_be_in_vocabulary():
    RequestedAttribute(type="color", raw="핑크색", value="핑크")
    RequestedAttribute(type="detail", raw="브이넥", value="브이넥")  # 넥라인 값도 detail
    with pytest.raises(ValidationError):
        RequestedAttribute(type="color", raw="x", value="분홍")


def test_trend_query_roundtrip():
    q = TrendQuery(
        request_id="r1",
        original_query="상의 기획안 작성해줘",
        trend_terms=TrendTerms(season=SeasonRequest.default_next(date(2026, 11, 6))),
    )
    assert TrendQuery.model_validate_json(q.model_dump_json()) == q


def test_item_derives_category_major_and_flags_conflict():
    item = ExtractedItem(
        name="재킷", raw_text="t", category_minor=V.CategoryMinor.JACKET, category_major=V.CategoryMajor.UPPER
    )
    assert item.category_major == V.CategoryMajor.OUTER
    assert any(f.startswith("category_major_mismatch") for f in item.review_flags)
    assert item.in_graph_scope
    assert not ExtractedItem(name="하이힐", raw_text="t", category_raw="하이힐").in_graph_scope


def test_item_size_is_always_none():
    with pytest.raises(ValidationError):
        ExtractedItem(name="x", raw_text="t", size="M")


def test_detail_value_must_match_type():
    ExtractedDetail(raw="브이넥", type=V.DetailType.NECKLINE, value="브이넥")
    ExtractedDetail(raw="후드", type=V.DetailType.DETAIL, value="후드")
    with pytest.raises(ValidationError):
        ExtractedDetail(raw="브이넥", type=V.DetailType.DETAIL, value="브이넥")


def test_focus_attributes_rules():
    f = lambda a, l: FocusAttribute(attribute=a, level=l, evidence=EV)  # noqa: E731
    ExtractedStyle(name="s", name_origin="article", definition="d",
                   focus_attributes=[f("color", "core"), f("detail", "supporting")])
    with pytest.raises(ValidationError):  # 근거 없음
        FocusAttribute(attribute="color", level="core", evidence=[])
    with pytest.raises(ValidationError):  # 같은 속성 반복
        ExtractedStyle(name="s", name_origin="article", definition="d",
                       focus_attributes=[f("color", "core"), f("color", "supporting")])
    with pytest.raises(ValidationError):  # 핵심 4개
        ExtractedStyle(name="s", name_origin="article", definition="d",
                       focus_attributes=[f("color", "core"), f("material", "core"), f("pattern", "core"), f("detail", "core"), f("fit", "supporting")])
    with pytest.raises(ValidationError):  # 허용되지 않은 속성
        FocusAttribute(attribute="length", level="core", evidence=EV)
    s = ExtractedStyle(name="s", name_origin="article", definition="d", focus_attributes=[f("category", "core")])
    assert s.focus_attributes[0].attribute.value == "category"   # 카테고리 자체가 트렌드인 경우


def _item(name, minor, colors=(), fit=None, patterns=()):
    return item_record_from_extraction(
        ExtractedItem(
            name=name, raw_text="t", category_minor=minor, fit=fit,
            colors=[{"raw": c, "value": c} for c in colors],
            patterns=[{"raw": p, "value": p} for p in patterns],
        ),
        item_id=name, article_id="a1",
    )


def test_style_profile_coverage_and_concentration():
    items = [
        _item("a", V.CategoryMinor.SHIRT, colors=["핑크"]),
        _item("b", V.CategoryMinor.SKIRT, colors=["핑크", "화이트"]),
        _item("c", V.CategoryMinor.JACKET, colors=["블랙"]),
        _item("d", V.CategoryMinor.COAT),
    ]
    p = compute_style_profile(items)
    assert p["item_count"] == 4
    assert p["coverage_color"] == 0.75
    assert p["concentration_color"] == pytest.approx(0.6667, abs=1e-4)  # 핑크 2 / 값이 있는 3
    assert p["coverage_fit"] == 0.0 and p["concentration_fit"] is None


def test_style_profile_ignores_out_of_scope_items_and_empty_style():
    out_of_scope = item_record_from_extraction(ExtractedItem(name="하이힐", raw_text="t", category_raw="하이힐"), "x", "a1")
    assert compute_style_profile([out_of_scope])["item_count"] == 0
    assert compute_style_profile([])["coverage_color"] is None


def test_record_drops_unmapped_values_and_flatten_skips_none():
    item = ExtractedItem(
        name="셔츠", raw_text="t", category_minor=V.CategoryMinor.SHIRT,
        colors=[{"raw": "오트밀", "value": None}, {"raw": "핑크색", "value": "핑크"}],
        fit=V.Fit.LOOSE,
    )
    rec = item_record_from_extraction(item, "i1", "a1", image_urls={})
    assert [c.value for c in rec.colors] == ["핑크"]
    flat = flatten(rec.node)
    assert flat["fit"] == "루즈" and "gender" not in flat and "size" not in flat


def test_approx_values_are_not_usable_in_first_search():
    from trend_agent.schemas.graph_schema import AttrEdge

    rec = _item("a", V.CategoryMinor.SHIRT)
    rec.materials = [AttrEdge(value="플리스", raw="기모", approx=True), AttrEdge(value="면", raw="코튼")]
    assert [e.value for e in rec.usable("material")] == ["면"]
    rec.node.fit, rec.node.approx = V.Fit.TIGHT, ["fit"]
    assert rec.usable_scalar("fit") is None


def test_style_node_focus_split():
    style = ExtractedStyle(
        name="맥시멀리즘", name_origin="article", definition="d",
        focus_attributes=[
            FocusAttribute(attribute="pattern", level="core", evidence=EV),
            FocusAttribute(attribute="detail", level="supporting", evidence=EV),
        ],
    )
    node = style_node_from_extraction(style, "s1")
    assert (node.focus_core, node.focus_supporting) == (["pattern"], ["detail"])


def test_style_seasons_are_a_list_and_unique():
    s = ExtractedStyle(
        name="보라색", name_origin="article", definition="d",
        seasons=[{"year": 2026, "season": "FW"}, {"year": 2027, "season": "SS"}],
    )
    assert [x.key for x in s.seasons] == ["2026FW", "2027SS"]
    assert style_node_from_extraction(s, "s1").seasons == ["2026FW", "2027SS"]
    with pytest.raises(ValidationError):
        ExtractedStyle(name="x", name_origin="article", definition="d",
                       seasons=[{"year": 2027, "season": "SS"}, {"year": 2027, "season": "SS"}])
    assert ExtractedStyle(name="x", name_origin="article", definition="d").seasons == []


def test_style_matches_season_by_membership():
    req = SeasonRequest(year=2027, season=V.Season.SS, origin="user_requested")
    assert style_matches_season(["2026FW", "2027SS"], req)        # 여러 시즌에 걸친 트렌드
    assert not style_matches_season(["2026FW"], req)              # 지난 시즌에만 유행한 트렌드는 보류
    assert not style_matches_season([], req)                      # 시즌 미확인은 보류


def test_result_models_construct():
    r = TrendAnalysisResult(request_id="r", status="no_results")
    assert r.trends == [] and r.limitations == []
    assert ArticleExtraction(article_id="a").styles == []
