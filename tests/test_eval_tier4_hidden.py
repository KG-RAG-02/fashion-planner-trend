"""eval_tier4_hidden: 가상 Item 값 합집합·focus·값 비교·겹침 (합성 입력)"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eval_tier4_hidden", ROOT / "scripts" / "eval_tier4_hidden.py")
th = importlib.util.module_from_spec(_spec)
sys.modules["eval_tier4_hidden"] = th
_spec.loader.exec_module(th)


def test_tier4_values_union_and_compare():
    items = [{"category_minor": "재킷", "colors": ["블랙"], "materials": ["벨벳"], "patterns": [], "details": [{"type": "detail", "name": "자수"}], "fit": None},
             {"category_minor": "스커트", "colors": ["블랙", "레드"], "materials": [], "patterns": [], "details": [], "fit": "루즈"}]
    v = th.tier4_values(items)
    assert v["category_minor"] == {"재킷", "스커트"} and v["color"] == {"블랙", "레드"} and v["detail"] == {"detail:자수"} and v["fit"] == {"루즈"}
    gold = {a: set() for a in th.VALUE_ATTRS} | {"color": {"블랙", "퍼플"}, "category_minor": {"스커트"}}
    c = th.compare_values(v, gold)
    assert (c["color|일치"], c["color|누락"], c["color|오추출"]) == (1, 1, 1) and c["category_minor|오추출"] == 1


def test_compare_focus_and_overlaps():
    c = th.compare_focus(["color", "material"], [], ["pattern"], ["color"])
    assert c == {"level 다름": 1, "오추출": 1, "누락": 1}
    assert th.overlaps(["a", "b", "c"], ["c", "x", "a"], ks=(1, 3)) == {1: 0, 3: 2}
