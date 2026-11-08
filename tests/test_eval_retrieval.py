"""eval_retrieval: 지표·풀 순위 복원·그룹·부트스트랩·κ·표본·HTML (합성 입력, Neo4j·이미지·Vogue 원문 없음)"""
import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eval_retrieval", ROOT / "scripts" / "eval_retrieval.py")
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)


def test_deep_merge_overrides_nested_without_mutating_base():
    base = {"category": {"major": "gate", "minor_bonus_alpha": 0.1}, "flags": {"snap_id_limit": 0}}
    out = er.deep_merge(base, {"category": {"minor_bonus_alpha": 0.5}, "flags": {"image_first_tiebreak": True}})
    assert out == {"category": {"major": "gate", "minor_bonus_alpha": 0.5}, "flags": {"snap_id_limit": 0, "image_first_tiebreak": True}}
    assert base["category"]["minor_bonus_alpha"] == 0.1


def test_ndcg_uses_judged_pool_ideal_and_treats_unjudged_and_null_as_zero():
    judged = {"a": 2, "b": 0, "c": 1, "d": 2, "e": None}
    ideal = er.dcg([2, 2, 1, 0])
    assert er.ndcg_at(["a", "b", "c", "x", "e"], judged) == pytest.approx(er.dcg([2, 0, 1, 0, 0]) / ideal)
    assert er.ndcg_at(["d", "a", "c"], judged) == pytest.approx(1.0)
    assert er.ndcg_at(["a"], {"a": 0, "b": None}) is None  # 관련 후보 없음
    assert er.dcg([2, 1]) == pytest.approx(3 + 1 / math.log2(3))


def test_item_metrics_counts_relevance_images_sources_gender():
    products = {"p1": {"has_image": True, "source": "musinsa_snap", "gender": None},
                "p2": {"has_image": False, "source": "otpensource", "gender": "여"},
                "p3": {"has_image": True, "source": "otpensource", "gender": "공용"},
                "p4": {"has_image": True, "source": "otpensource", "gender": "남"}}
    m = er.item_metrics(["p1", "p2", "p3", "p4"], {"p1": 2, "p2": 1, "p3": None}, products, "남")
    assert (m["n"], m["rel2"], m["rel1p"], m["unjudged"], m["null"]) == (4, 1, 2, 1, 1)
    assert (m["image"], m["snap"], m["catalog"]) == (3, 1, 3)
    assert (m["gender_일치"], m["gender_불일치"], m["gender_미상"]) == (2, 1, 1)
    assert "gender_일치" not in er.item_metrics(["p1"], {}, products, None)


def test_rankings_from_pool_and_groups():
    rows = [{"item_id": "1-s0-i0", "product_id": "b", "hits": [{"config": "P0", "rank": 2, "score": .5}, {"config": "P1", "rank": 1, "score": .7}]},
            {"item_id": "1-s0-i0", "product_id": "a", "hits": [{"config": "P0", "rank": 1, "score": .9}]}]
    assert er.rankings_from_pool(rows) == {"P0": {"1-s0-i0": ["a", "b"]}, "P1": {"1-s0-i0": ["b"]}}
    split = {"1": "dev", "2": "test"}
    assert [er.item_group(i, split) for i in ("1-s0-i0", "2-s3-i1", "virtual:4:드뮤어-i0", "9-s0-i0")] == ["dev Style", "test Style", "Tier4 프로필", "기타"]


def test_compute_metrics_vs_baseline_and_bootstrap():
    items = {f"1-s0-i{k}": {"gender": None} for k in range(3)}
    judg = {i: {"g": 2, "b": 0} for i in items}
    rankings = {"P0": {i: ["b", "g"] for i in items}, "P1": {i: ["g", "b"] for i in items}}
    res = er.compute_metrics(rankings, judg, {}, items, {"1": "dev"}, "P0")
    assert res["P1"]["vs_baseline"]["좋아짐"] == 3 and res["P1"]["vs_baseline"]["나빠짐"] == 0
    assert res["P1"]["dev Style"]["ndcg_mean"] == 1.0 and res["P0"]["전체"]["rel2"] == 3
    assert "vs_baseline" not in res["P0"]
    lo, hi = er.bootstrap_ci([0.1, 0.2, 0.3], n=500)
    assert 0.1 <= lo <= hi <= 0.3 and er.bootstrap_ci([]) is None


def test_weighted_kappa_and_review_score():
    assert er.weighted_kappa([(0, 0), (1, 1), (2, 2), (2, 2)]) == pytest.approx(1.0)
    assert er.weighted_kappa([]) is None
    c3 = [{"item_id": "i", "product_id": p, "grade": g, "pass": 1} for p, g in (("a", 2), ("b", 0), ("c", None), ("d", 1))]
    user = [{"item_id": "i", "product_id": "a", "grade": 2}, {"item_id": "i", "product_id": "b", "grade": 1},
            {"item_id": "i", "product_id": "c", "grade": None}, {"item_id": "i", "product_id": "d", "grade": "미입력"}]
    r = er.review_score(user, c3)
    assert (r["쌍"], r["일치"], r["불일치"], r["κ 쌍 수"]) == (3, 2, 1, 2)
    assert r["혼동표(C3→사용자)"] == {"0→1": 1, "2→2": 1, "None→None": 1}


def test_review_sample_takes_per_grade_plus_all_null_deterministically():
    rows = [{"item_id": f"i{n}", "product_id": "p", "grade": g, "pass": 1} for n, g in enumerate([0] * 15 + [1] * 3 + [2] * 12 + [None] * 4)]
    rows.append({"item_id": "x", "product_id": "p", "grade": 2, "pass": 2})  # 재판정은 표본에서 뺀다
    s1, s2 = er.review_sample(rows, 10), er.review_sample(rows, 10)
    assert s1 == s2 and len(s1) == 10 + 3 + 10 + 4
    assert sum(1 for r in s1 if r["grade"] is None) == 4 and all(r["pass"] == 1 for r in s1)


def test_review_html_hides_grades_and_is_self_contained():
    sample = [{"item_id": "1-s0-i0", "product_id": "p", "grade": 2}]
    out = er.review_html(sample, {"1-s0-i0": {"style_name": "합성", "name": "셔츠", "header": "상의>셔츠"}}, {"p": {"card_text": "상품"}})
    assert "결과 복사" in out and "data-item=\"1-s0-i0\"" in out and "이미지 없음" in out
    assert '"grade": 2' not in out and "grade:" in out  # C3 등급은 넣지 않는다(입력 수집 코드에만 grade 키)
    assert "alert(" not in out and "<script src" not in out


def test_count_command(tmp_path, capsys):
    rows = [{"item_id": "a", "product_id": str(k), "hits": []} for k in range(25)] + [{"item_id": "b", "product_id": "x", "hits": []}]
    (tmp_path / "pool.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert er.main(["count", "--pool", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": 2, "pairs": 26, "sheets": 3, "max_per_item": 25}
