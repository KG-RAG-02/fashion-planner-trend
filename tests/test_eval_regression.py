"""eval_regression: 출력 파싱·기준선 비교·기준선 갱신 (Neo4j·하위 프로세스 없이 합성 입력)"""
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eval_regression", ROOT / "scripts" / "eval_regression.py")
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)

BASE = {"updated_at": "2026-11-08", "reason": "r", "graph": {"Article": 23, "Style": 31, "Item": 106, "Product": 33189, "Term": 8},
        "verify_graph_search": {"top_k": 5, "items": 106, "mismatches": 0}, "pytest": {"passed": 113}}


def test_parse_pytest_and_verify_outputs():
    assert er.parse_pytest("....\n113 passed in 0.41s") == {"passed": 113, "failed": 0, "errors": 0, "skipped": 0}
    assert er.parse_pytest("2 failed, 100 passed, 1 skipped, 3 errors in 1s") == {"passed": 100, "failed": 2, "errors": 3, "skipped": 1}
    assert er.parse_verify("OK  a / b\n\n106개 Item 중 불일치 0개\n") == {"items": 106, "mismatches": 0}
    assert er.parse_verify("Traceback ...") is None


def test_compare_passes_on_baseline_and_flags_regressions():
    ok = {"pytest": {"passed": 120, "failed": 0, "errors": 0}, "verify": {"items": 106, "mismatches": 0}, "graph": dict(BASE["graph"])}
    assert all(r[-1] for r in er.compare(ok, BASE))
    bad = {"pytest": {"passed": 90, "failed": 1, "errors": 0}, "verify": {"items": 105, "mismatches": 2},
           "graph": dict(BASE["graph"], Style=30)}
    failed = {r[0] for r in er.compare(bad, BASE) if not r[-1]}
    assert failed == {"pytest 실패·오류", "verify 불일치", "verify Item 수", "그래프 Style"}
    assert [r for r in er.compare({"verify": {"error": "x"}}, BASE)][0][-1] is False


def test_update_baseline_keeps_history_and_reason(tmp_path):
    p = tmp_path / "baseline.yaml"
    measured = {"top_k": 5, "pytest": {"passed": 130, "failed": 0, "errors": 0}, "verify": {"items": 120, "mismatches": 0},
                "graph": dict(BASE["graph"], Item=120)}
    new = er.update_baseline(p, dict(BASE), measured, "추출 적재", "abc1234")
    disk = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert disk == new and new["reason"] == "추출 적재" and new["graph"]["Item"] == 120 and new["measured_at_git"] == "abc1234"
    assert new["verify_graph_search"] == {"top_k": 5, "items": 120, "mismatches": 0} and new["pytest"] == {"passed": 130}
    assert new["history"][0]["graph"]["Item"] == 106 and new["history"][0]["reason"] == "r"
    assert p.read_text(encoding="utf-8").startswith("# 회귀 기준선")
