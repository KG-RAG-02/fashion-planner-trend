"""점수 계산 (합성 데이터). 규칙: docs/architecture.md("점수 계산")"""
import pytest

from fashion_planner_trend.retrieval.scoring import (
    attribute_values,
    check_requested,
    item_passes,
    product_passes,
    rank_products,
    score_product,
)
from fashion_planner_trend.retrieval.weights import load_weights
from fashion_planner_trend.schemas.graph_schema import AttrEdge, DetailEdge, ItemNode, ItemRecord, ProductNode, ProductRecord
from fashion_planner_trend.schemas.trend_query import RequestedAttribute

W = load_weights()


def item(major="하의", minor="스커트", fit=None, approx=(), **edges):
    return ItemRecord(
        node=ItemNode(id="i", name="n", raw_text="t", article_id="a", category_minor=minor, fit=fit, approx=list(approx)),
        category_major=major, **edges)


def prod(pid="p", major="하의", minor="스커트", fit=None, approx=(), **edges):
    return ProductRecord(
        node=ProductNode(id=pid, source="otpensource", kind="catalog_item", category_minor=minor, fit=fit,
                         source_row=pid, approx=list(approx)),
        category_major=major, **edges)


def c(v, approx=False):
    return AttrEdge(value=v, approx=approx)


def test_gate_requires_same_major():
    assert score_product(item(), prod(major="상의"), W) is None


def test_weighted_ratio_and_minor_bonus():
    it = item(colors=[c("그레이")], patterns=[c("스트라이프")])
    p = prod(colors=[c("그레이")], patterns=[c("체크")])
    s = score_product(it, p, W)
    # color 3 × 1 + pattern 2 × 0 = 3 / 5 = 0.6, 하분류 일치 +0.1
    assert s.breakdown.base_score == pytest.approx(0.6) and s.score == pytest.approx(0.7)
    assert s.match.matched == ["color:그레이"] and s.match.mismatched == ["pattern:스트라이프"]


def test_partial_ratio_for_multiple_values():
    s = score_product(item(colors=[c("블랙"), c("화이트")]), prod(colors=[c("블랙")]), W)
    assert s.breakdown.attributes["color"].ratio == 0.5


def test_item_without_value_is_excluded_not_penalized():
    only_color = score_product(item(colors=[c("블랙")]), prod(colors=[c("블랙")], patterns=[c("체크")]), W)
    assert list(only_color.breakdown.attributes) == ["color"] and only_color.breakdown.base_score == 1.0


def test_product_without_value_is_unknown_and_not_compared():
    s = score_product(item(colors=[c("블랙")], materials=[c("가죽")]), prod(colors=[c("블랙")]), W)
    assert s.match.unknown == ["material"] and s.breakdown.base_score == 1.0


def test_no_comparable_attributes_gives_bonus_only():
    s = score_product(item(), prod(), W)
    assert s.breakdown.base_score == 0.0 and s.score == pytest.approx(0.1)
    assert score_product(item(), prod(minor="팬츠"), W).score == 0.0   # 하분류 불일치·무속성은 0, 감점 없음


def test_approx_values_are_ignored():
    it = item(colors=[c("블랙")])
    p = prod(colors=[c("블랙", approx=True)])
    assert attribute_values(p, "color") == set()
    assert score_product(it, p, W).match.unknown == ["color"]
    # 근사 핏도 쓰지 않는다
    assert attribute_values(prod(fit="타이트", approx=["fit"]), "fit") == set()


def test_fit_and_detail_ids():
    it = item(fit="스키니", details=[DetailEdge(type="detail", value="후드")])
    p = prod(fit="스키니", details=[DetailEdge(type="neckline", value="후드")])
    s = score_product(it, p, W)
    assert s.breakdown.attributes["fit"].ratio == 1 and s.breakdown.attributes["detail"].ratio == 0  # detail:후드 ≠ neckline:후드


def test_trend_focus_changes_weights_and_category_alpha():
    it = item(colors=[c("블랙")], patterns=[c("체크")])
    p = prod(colors=[c("블랙")], patterns=[c("스트라이프")])
    base = score_product(it, p, W).breakdown.base_score          # 3 / 5
    focused = score_product(it, p, W, focus_core=["color"]).breakdown.base_score   # 6 / 8
    assert focused > base and focused == pytest.approx(0.75)
    assert score_product(it, p, W, focus_core=["category"]).breakdown.bonus["category_minor"] == pytest.approx(0.2)


def test_ranking_is_deterministic_and_drops_zero_scores():
    it = item(colors=[c("블랙")])
    ps = [prod("p3", colors=[c("블랙")]), prod("p1", colors=[c("블랙")]), prod("p2", colors=[c("화이트")], minor="팬츠")]
    ranked = rank_products(it, ps, W)
    assert [r.product.node.id for r in ranked] == ["p1", "p3"]       # 동점은 id 순, 점수 0인 p2는 제외


def test_requested_filters_item_and_product_rules():
    req = [RequestedAttribute(type="color", raw="검정", value="블랙"), RequestedAttribute(type="color", raw="흰색", value="화이트")]
    ok, res = item_passes(item(colors=[c("블랙")]), req)
    assert ok and res.matched == ["color:블랙"]                       # 같은 속성의 여러 값은 OR
    assert item_passes(item(), req)[0] and item_passes(item(), req)[1].unknown == ["color"]   # 미확인 Item은 통과
    assert item_passes(item(colors=[c("레드")]), req)[0] is False     # 명시적으로 다르면 제거
    assert product_passes(prod(colors=[c("화이트")]), req) == (True, "ok")
    assert product_passes(prod(), req) == (False, "unknown")          # 미확인 Product는 제외
    assert product_passes(prod(colors=[c("레드")]), req) == (False, "mismatch")
    # 표준 값으로 못 바꾼 요청은 필터에 쓰지 않는다
    assert check_requested(prod(), [RequestedAttribute(type="color", raw="바닐라")]) .unknown == []
