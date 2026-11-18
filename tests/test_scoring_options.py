"""점수 선택 사항: 값 가중 재현율. 값 가중치가 모두 1.0이면 값 가중치가 없을 때와 결과가 같아야 한다."""
import random

import pytest

from fashion_planner_trend.retrieval.scoring import (
    ScoringOptions,
    attribute_values,
    score_product,
)
from fashion_planner_trend.retrieval.weights import load_weights
from fashion_planner_trend.schemas import vocabulary as V
from fashion_planner_trend.schemas.graph_schema import (
    SCORED_ATTRIBUTES,
    AttrEdge,
    DetailEdge,
    ItemNode,
    ItemRecord,
    ProductNode,
    ProductRecord,
)

W = load_weights()


# ---------- 합성 데이터 ----------
COLORS = [m.value for m in V.Color][:8]
MATERIALS = [m.value for m in V.Material][:6]
PATTERNS = [m.value for m in V.Pattern][:5]
DETAILS = [(t, m.value) for t, enum in V.DETAIL_ENUMS.items() for m in list(enum)[:2]][:8]
FITS = [m.value for m in V.Fit]
MAJORS = [m.value for m in V.CategoryMajor]
MINORS = {"상의": ["셔츠", "티셔츠"], "하의": ["스커트", "팬츠"], "아우터": ["재킷", "코트"], "원피스": ["원피스"]}
FOCUS = ["color", "material", "pattern", "detail", "fit", "category"]


def _edges(rng, pool, n_max):
    return [AttrEdge(value=v, approx=rng.random() < 0.15) for v in rng.sample(pool, rng.randint(0, n_max))]


def _details(rng):
    return [DetailEdge(type=t, value=v, approx=rng.random() < 0.15) for t, v in rng.sample(DETAILS, rng.randint(0, 3))]


def _minor(rng, major):
    opts = [m for m in MINORS[major] if m in {x.value for x in V.CategoryMinor}]
    return rng.choice(opts + [None]) if opts else None


def rand_item(rng, k):
    major = rng.choice(MAJORS)
    fit = rng.choice(FITS + [None])
    approx = ["fit"] if fit and rng.random() < 0.2 else []
    return ItemRecord(
        node=ItemNode(id=f"i{k}", name="n", raw_text="t", article_id="a", category_minor=_minor(rng, major), fit=fit, approx=approx),
        category_major=major, colors=_edges(rng, COLORS, 3), materials=_edges(rng, MATERIALS, 2),
        patterns=_edges(rng, PATTERNS, 2), details=_details(rng))


def rand_product(rng, k, major=None):
    major = major or rng.choice(MAJORS)
    fit = rng.choice(FITS + [None])
    approx = ["fit"] if fit and rng.random() < 0.2 else []
    return ProductRecord(
        node=ProductNode(id=f"p{k}", source="otpensource", kind="catalog_item", category_minor=_minor(rng, major),
                         fit=fit, source_row=str(k), approx=approx),
        category_major=major, colors=_edges(rng, COLORS, 3), materials=_edges(rng, MATERIALS, 2),
        patterns=_edges(rng, PATTERNS, 2), details=_details(rng))


def test_all_one_value_weights_equal_default_on_random_pairs():
    rng = random.Random(20261108)
    n = 0
    for k in range(5000):
        it = rand_item(rng, k)
        p = rand_product(rng, k, major=it.category_major.value if rng.random() < 0.85 else None)
        core = rng.sample(FOCUS, rng.randint(0, 3))
        sup = rng.sample([f for f in FOCUS if f not in core], rng.randint(0, 2))
        ones = {a: {v: 1.0 for v in attribute_values(it, a)} for a in SCORED_ATTRIBUTES}
        ref = score_product(it, p, W, core, sup)
        got = score_product(it, p, W, core, sup, ScoringOptions(value_weights=ones))
        if ref is None:
            assert got is None
            continue
        n += 1
        assert got.score == ref.score and got.compared == ref.compared and got.match == ref.match
        assert got.breakdown.attributes == ref.breakdown.attributes and got.breakdown.bonus == ref.breakdown.bonus
    assert n > 3000  # 같은 상분류 쌍이 충분히 비교됐다


def test_value_weighted_ratio():
    """가상 Item: color core 베이지(가중치 1.0)·화이트(0.5), material supporting 면, fit 루즈."""
    it = ItemRecord(node=ItemNode(id="v", name="v", raw_text="", article_id="virtual", fit="루즈"),
                    category_major="상의", colors=[AttrEdge(value="베이지"), AttrEdge(value="화이트")],
                    materials=[AttrEdge(value="면")])
    snap = ProductRecord(node=ProductNode(id="musinsa_snap:1", source="musinsa_snap", kind="snap_garment", fit="루즈", source_row="1"),
                         category_major="상의", materials=[AttrEdge(value="면")])
    cat = ProductRecord(node=ProductNode(id="otpensource:1", source="otpensource", kind="catalog_item", source_row="1"),
                        category_major="상의", colors=[AttrEdge(value="베이지")])
    opt = ScoringOptions(value_weights={"color": {"베이지": 1.0, "화이트": 0.5}})
    a = score_product(it, snap, W, ["color"], ["material"], opt)
    b = score_product(it, cat, W, ["color"], ["material"], opt)
    assert a.score == pytest.approx(1.0) and a.match.unknown == ["color"]  # 상품에 없는 color는 분모에서 빠진다
    assert b.score == pytest.approx(2 / 3)  # 베이지 1.0 ÷ (1.0 + 0.5)
    assert b.breakdown.value_weighted and b.breakdown.attributes["color"].ratio == pytest.approx(2 / 3)


def _p(pid, image_status=None, image_url=None, snap_id=None, gender=None, color="블랙"):
    return ProductRecord(node=ProductNode(id=pid, source="otpensource", kind="catalog_item", source_row=pid,
                                          image_status=image_status, image_url=image_url, snap_id=snap_id, gender=gender),
                         category_major="상의", colors=[AttrEdge(value=color)])


def test_has_image_definition():
    assert _p("x", image_status="ok").node.has_image
    assert not _p("x", image_status="broken", image_url="u").node.has_image
    assert _p("x", image_url="u").node.has_image  # status 없으면 참조 존재
    assert not _p("x").node.has_image


def test_weights_sha_is_stable():
    assert len(W.sha) == 12 and W.sha == load_weights().sha
