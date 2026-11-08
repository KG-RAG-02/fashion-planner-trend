"""tune_weights: 벡터화 점수·정렬이 운영 rank_products와 완전히 같은지(합성 레코드, 무작위 설정) + 격자·선택 규칙"""
import importlib.util
import random
import sys
from pathlib import Path

from trend_agent.retrieval.scoring import ScoringOptions, rank_products
from trend_agent.retrieval.weights import WeightsConfig
from trend_agent.schemas.graph_schema import AttrEdge, DetailEdge, ItemNode, ItemRecord, ProductNode, ProductRecord

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tune_weights", ROOT / "scripts" / "tune_weights.py")
tw = importlib.util.module_from_spec(_spec)
sys.modules["tune_weights"] = tw  # dataclass가 모듈을 찾는다
_spec.loader.exec_module(tw)

COLORS, MATS, PATS, FITS = ["블랙", "화이트", "핑크", "블루"], ["면", "데님", "가죽"], ["체크", "도트"], ["루즈", "와이드", "스키니"]
MINORS = ["팬츠", "청바지", "스커트"]


def _edges(rng, pool, n):
    return [AttrEdge(value=v) for v in rng.sample(pool, rng.randint(0, n))]


def make(rng, n=60):
    it = ItemRecord(node=ItemNode(id="i", name="n", raw_text="t", article_id="a", category_minor=rng.choice(MINORS),
                                  fit=rng.choice(FITS + [None]), gender=rng.choice(["남", "여", None])),
                    category_major="하의", colors=_edges(rng, COLORS, 2), materials=_edges(rng, MATS, 1), patterns=_edges(rng, PATS, 1),
                    details=[DetailEdge(type="detail", value="포켓")] if rng.random() < .5 else [])
    prods = []
    for k in range(n):
        snap = rng.choice([None, 1, 2, 3])
        prods.append(ProductRecord(
            node=ProductNode(id=f"p{k:03d}", source="musinsa_snap" if snap else "otpensource", kind="snap_garment" if snap else "catalog_item",
                             category_minor=rng.choice(MINORS + [None]), fit=rng.choice(FITS + [None]), gender=rng.choice(["남", "여", "공용", None]),
                             source_row=str(k), snap_id=snap, image_status=rng.choice(["ok", "broken", None])),
            category_major="하의", colors=_edges(rng, COLORS, 2), materials=_edges(rng, MATS, 1), patterns=_edges(rng, PATS, 1),
            details=[DetailEdge(type="detail", value="포켓")] if rng.random() < .3 else []))
    return it, prods


def test_vectorized_rank_equals_rank_products_on_random_settings():
    base = tw.ER.weights_dict()
    grid = tw.load_grid()
    rng = random.Random(7)
    settings = [tw.setting_from_weights(base)] + rng.sample(grid, 40)
    checked = 0
    for trial in range(6):
        it, prods = make(random.Random(trial))
        core, sup = random.Random(trial).sample([["color"], ["fit", "category"], [], ["pattern"]], 2)
        entry = {"item": it, "style_id": "s", "core": core, "sup": sup}
        comp = tw.components(entry, prods)
        for s in settings:
            w = WeightsConfig.model_validate(tw.weights_from_setting(s, base))
            ref = [(x.product.node.id, round(x.score, 9)) for x in rank_products(it, prods, w, core, sup, top_k=20, options=ScoringOptions.from_weights(w))]
            assert [(p, round(v, 9)) for p, v in tw.rank(comp, s, 20)] == ref, s
            checked += 1
    assert checked == 6 * 41


def test_grid_contains_current_weights_and_select_applies_constraints():
    grid = tw.load_grid()
    assert len(grid) == 1280 and tw.setting_from_weights(tw.ER.weights_dict()) in grid
    base = {"m": {"image": 10, "gender_불일치": 2}}
    rows = [{"setting": {"n": 1}, "m": {"coverage": 0.95, "ndcg_mean": 0.8, "image": 9, "gender_불일치": 0}},   # 이미지 제약 위반
            {"setting": {"n": 2}, "m": {"coverage": 0.85, "ndcg_mean": 0.9, "image": 12, "gender_불일치": 0}},  # 커버리지 미달
            {"setting": {"n": 3}, "m": {"coverage": 0.92, "ndcg_mean": 0.7, "image": 10, "gender_불일치": 2}},
            {"setting": {"n": 4}, "m": {"coverage": 0.99, "ndcg_mean": 0.75, "image": 11, "gender_불일치": 1}}]
    assert [r["setting"]["n"] for r in tw.select(rows, base)] == [4, 3]
