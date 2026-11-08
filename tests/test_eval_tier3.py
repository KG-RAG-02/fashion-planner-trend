"""eval_tier3: 수용 판정·결과 분류·격자 건수·선택 규칙·반반 분할 (합성 후보 점수)"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eval_tier3", ROOT / "scripts" / "eval_tier3.py")
t3 = importlib.util.module_from_spec(_spec)
sys.modules["eval_tier3"] = t3
_spec.loader.exec_module(t3)


def C(*pairs):
    return [{"group": g, "score": s} for g, s in pairs]


def test_decide_and_outcome():
    assert t3.decide(C(("시럽코어", .8), ("핑크", .6)), .7, .1) == "시럽코어"
    assert t3.decide(C(("시럽코어", .8), ("핑크", .75)), .7, .1) is None     # 1·2위 차이 부족
    assert t3.decide(C(("시럽코어", .65)), .7, 0) is None and t3.decide([], .1, 0) is None
    assert t3.outcome(["시럽 코어"], "시럽코어") == "TA"                       # 정규화 비교
    assert (t3.outcome(["시럽코어"], "핑크"), t3.outcome(["시럽코어"], None)) == ("FA", "FR")
    assert (t3.outcome([], None), t3.outcome([], "핑크")) == ("TR", "FA")


ROWS = [{"query": "a", "expected": ["시럽코어"], "kind": "exact", "cands": C(("시럽코어", .90), ("핑크", .50))},
        {"query": "b", "expected": ["뉴 로"], "kind": "translation", "cands": C(("뉴로", .62), ("핑크", .55))},
        {"query": "c", "expected": [], "kind": "confusable", "cands": C(("절제된맥시멀리즘", .70), ("핑크", .40))},
        {"query": "d", "expected": [], "kind": "unrelated", "cands": C(("핑크", .35), ("뉴로", .34))}]


def test_grid_counts_and_selection_respects_zero_false_accept_on_negatives():
    counts = t3.grid_counts(ROWS, taus=[0.3, 0.6, 0.71, 0.8], deltas=[0.0, 0.05])
    assert counts[(0.3, 0.0)] == {"TA": 2, "FA": 2, "TR": 0, "FR": 0, "FA_neg": 2}
    assert counts[(0.71, 0.0)] == {"TA": 1, "FA": 0, "TR": 2, "FR": 1, "FA_neg": 0}
    pick = t3.select(counts)
    assert counts[pick]["FA_neg"] == 0 and counts[pick]["TA"] == 1
    assert t3.select({(0.3, 0.0): {"TA": 1, "FA": 1, "TR": 0, "FR": 0, "FA_neg": 1}}) is None


def test_split_halves_is_deterministic_and_complete():
    rows = [{"query": str(i)} for i in range(9)]
    a, b = t3.split_halves(rows)
    assert (a, b) == t3.split_halves(rows) and len(a) == 4 and sorted(r["query"] for r in a + b) == sorted(r["query"] for r in rows)
