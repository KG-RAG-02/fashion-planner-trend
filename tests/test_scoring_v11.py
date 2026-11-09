"""점수 v1.1 (값 가중 재현율 · γ · 이미지 동점 · snap 제한 · 성별 필터). 기본값은 v1.0과 같아야 한다.

회귀 기준: v1.0 `score_product`(develop@3834420)의 식을 아래 `_score_v10`에 그대로 얼려 두고, 무작위 합성 쌍에서
점수·비교 수·breakdown·match가 **완전히 같음**(값 가중치 없음 / 모두 1.0)을 확인한다.
"""
import random

import pytest

from trend_agent.retrieval.scoring import (
    ScoringOptions,
    attribute_values,
    category_minor_of,
    gender_compatible,
    rank_products,
    score_product,
)
from trend_agent.retrieval.weights import WeightsConfig, load_weights
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import (
    SCORED_ATTRIBUTES,
    AttrEdge,
    DetailEdge,
    ItemNode,
    ItemRecord,
    ProductNode,
    ProductRecord,
)

W = load_weights()


# ---------- v1.0 기준 식 (얼린 사본 — 바꾸지 않는다) ----------
def _score_v10(item, product, weights, focus_core=(), focus_supporting=()):
    if item.category_major is None or item.category_major != product.category_major:
        return None
    core, supporting = set(focus_core), set(focus_supporting)
    attrs, matched, mismatched, unknown = {}, [], [], []
    num = den = 0.0
    for attr in SCORED_ATTRIBUTES:
        iv = attribute_values(item, attr)
        if not iv:
            continue
        pv = attribute_values(product, attr)
        if not pv:
            unknown.append(attr)
            continue
        common = iv & pv
        ratio = len(common) / len(iv)
        w = weights.applied_weight(attr, core, supporting)
        attrs[attr] = (w, weights.base_weights[attr], "core" if attr in core else "supporting" if attr in supporting else None, ratio)
        num += w * ratio
        den += w
        matched += [v if attr == "detail" else f"{attr}:{v}" for v in sorted(common)]
        mismatched += [v if attr == "detail" else f"{attr}:{v}" for v in sorted(iv - pv)]
    base = num / den if den else 0.0
    bonus = {}
    im, pm = category_minor_of(item), category_minor_of(product)
    if im is not None and im == pm:
        bonus["category_minor"] = weights.applied_alpha(core, supporting)
    return base + sum(bonus.values()), base, bonus, attrs, (matched, mismatched, unknown), len(attrs)


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


@pytest.mark.parametrize("all_ones", [False, True])
def test_v11_defaults_equal_v10_on_random_pairs(all_ones):
    rng = random.Random(20261108)
    n = 0
    for k in range(5000):
        it = rand_item(rng, k)
        p = rand_product(rng, k, major=it.category_major.value if rng.random() < 0.85 else None)
        core = rng.sample(FOCUS, rng.randint(0, 3))
        sup = rng.sample([f for f in FOCUS if f not in core], rng.randint(0, 2))
        vw = None
        if all_ones:
            vw = {a: {v: 1.0 for v in attribute_values(it, a)} for a in SCORED_ATTRIBUTES}
        ref = _score_v10(it, p, W, core, sup)
        got = score_product(it, p, W, core, sup, ScoringOptions(value_weights=vw))
        if ref is None:
            assert got is None
            continue
        n += 1
        score, base, bonus, attrs, (m, mm, u), compared = ref
        assert got.score == score and got.breakdown.base_score == base and got.breakdown.bonus == bonus
        assert {a: (s.weight, s.base_weight, s.level, s.ratio) for a, s in got.breakdown.attributes.items()} == attrs
        assert (got.match.matched, got.match.mismatched, got.match.unknown) == (m, mm, u)
        assert got.compared == compared and got.breakdown.unknown_core_weight == 0.0
    assert n > 3000  # 같은 상분류 쌍이 충분히 비교됐다


def _gamma_case(gamma):
    """설계 메모 §2.6: 가상 Item (color core 베이지 primary·화이트 secondary, material supporting 면, fit 루즈)."""
    it = ItemRecord(node=ItemNode(id="v", name="v", raw_text="", article_id="virtual", fit="루즈"),
                    category_major="상의", colors=[AttrEdge(value="베이지"), AttrEdge(value="화이트")],
                    materials=[AttrEdge(value="면")])
    snap = ProductRecord(node=ProductNode(id="musinsa_snap:1", source="musinsa_snap", kind="snap_garment", fit="루즈", source_row="1"),
                         category_major="상의", materials=[AttrEdge(value="면")])
    cat = ProductRecord(node=ProductNode(id="otpensource:1", source="otpensource", kind="catalog_item", source_row="1"),
                        category_major="상의", colors=[AttrEdge(value="베이지")])
    opt = ScoringOptions(value_weights={"color": {"베이지": 1.0, "화이트": 0.5}}, core_unknown_decay=gamma)
    a = score_product(it, snap, W, ["color"], ["material"], opt)
    b = score_product(it, cat, W, ["color"], ["material"], opt)
    return a, b


@pytest.mark.parametrize("gamma,snap_score", [(0.0, 1.0), (0.5, 0.625), (1.0, 5 / 11)])
def test_core_unknown_decay(gamma, snap_score):
    snap, cat = _gamma_case(gamma)
    assert snap.score == pytest.approx(snap_score)
    assert cat.score == pytest.approx(2 / 3)  # 베이지 1.0 ÷ (1.0 + 0.5)
    assert snap.breakdown.unknown_core_weight == pytest.approx(gamma * 6.0)  # color 3 × core 2.0
    assert cat.breakdown.value_weighted and cat.breakdown.attributes["color"].ratio == pytest.approx(2 / 3)


def _p(pid, image_status=None, image_url=None, snap_id=None, gender=None, color="블랙"):
    return ProductRecord(node=ProductNode(id=pid, source="otpensource", kind="catalog_item", source_row=pid,
                                          image_status=image_status, image_url=image_url, snap_id=snap_id, gender=gender),
                         category_major="상의", colors=[AttrEdge(value=color)])


def _it(gender=None):
    return ItemRecord(node=ItemNode(id="i", name="n", raw_text="", article_id="a", gender=gender),
                      category_major="상의", colors=[AttrEdge(value="블랙")])


def test_image_first_tiebreak_only_when_flag_on():
    ps = [_p("a"), _p("b", image_status="ok", image_url="u"), _p("c", image_status="broken", image_url="u")]
    assert [s.product.node.id for s in rank_products(_it(), ps, W)] == ["a", "b", "c"]
    on = ScoringOptions(image_first_tiebreak=True)
    assert [s.product.node.id for s in rank_products(_it(), ps, W, options=on)] == ["b", "a", "c"]


def test_has_image_definition():
    assert _p("x", image_status="ok").node.has_image
    assert not _p("x", image_status="broken", image_url="u").node.has_image
    assert _p("x", image_url="u").node.has_image  # status 없으면 참조 존재
    assert not _p("x").node.has_image


def test_snap_id_limit():
    ps = [_p("a", snap_id=7), _p("b", snap_id=7), _p("c", snap_id=8), _p("d")]
    stats: dict = {}
    got = rank_products(_it(), ps, W, options=ScoringOptions(snap_id_limit=1), stats=stats)
    assert [s.product.node.id for s in got] == ["a", "c", "d"] and stats["snap_limited"] == 1
    assert len(rank_products(_it(), ps, W)) == 4


def test_item_gender_filter():
    ps = [_p("m", gender="남"), _p("f", gender="여"), _p("u", gender="공용"), _p("n")]
    stats: dict = {}
    got = rank_products(_it("여"), ps, W, options=ScoringOptions(item_gender_filter=True), stats=stats)
    assert [s.product.node.id for s in got] == ["f", "n", "u"] and stats["gender_excluded"] == 1
    assert len(rank_products(_it("여"), ps, W)) == 4  # 기본 꺼짐 (G0-③)
    assert gender_compatible(_it("공용"), ps[0]) and gender_compatible(_it(None), ps[0])


def test_weights_v11_sections_default_to_v10_when_absent():
    cfg = WeightsConfig.model_validate({"base_weights": dict(W.base_weights)})
    assert cfg.flags.core_unknown_decay == 0.0
    assert not cfg.flags.image_first_tiebreak and cfg.flags.snap_id_limit == 0 and not cfg.flags.item_gender_filter
    assert ScoringOptions.from_weights(cfg) == ScoringOptions()


def test_weights_yaml_flags_are_all_off_and_sha():
    f = W.flags  # 켜는 것은 사용자 결정 사항이다. 저장소 설정은 v1.0 동작(모두 꺼짐)이다.
    assert (f.image_first_tiebreak, f.core_unknown_decay, f.snap_id_limit, f.item_gender_filter) == (False, 0.0, 0, False)
    assert ScoringOptions.from_weights(W) == ScoringOptions()
    assert len(W.sha) == 12 and W.sha == load_weights().sha
