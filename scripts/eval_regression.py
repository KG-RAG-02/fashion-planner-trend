"""회귀 점검: pytest · verify_graph_search(플래그 기본) · 그래프 노드 수를 기준선(eval/regression/baseline.yaml)과 비교한다.

실행 (레포 루트, 공유 venv):
  python scripts/eval_regression.py [--repo <코드를 돌릴 저장소/worktree>] [--skip-neo4j] [--skip-pytest]
  python scripts/eval_regression.py --update-baseline --reason "라벨 23건 재적재"   # 측정값을 기준선으로 (사유·날짜 기록 후 커밋)

- pytest와 verify_graph_search는 --repo(기본: 이 저장소)에서 돈다. verify는 그 저장소의 코드·data/processed 링크를 쓰므로,
  그래프를 적재한 코드와 같은 저장소를 고른다(예: develop 병합 전에는 T3 worktree).
- 그래프 수는 읽기 전용 Cypher(count)로만 센다. Neo4j 쓰기·적재는 하지 않는다(T3·L0 전용).
- 종료 코드: 0 = 전부 통과, 1 = 회귀(실패·불일치·수 변화), 2 = 실행 불가(접속 실패 등).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "eval" / "regression" / "baseline.yaml"
LABELS = ("Article", "Style", "Item", "Product", "Term")
PYTHON = sys.executable
# 다른 worktree(--repo)에 캐시 파일을 남기지 않는다
ENV = os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}


def parse_pytest(out: str) -> dict:
    """pytest -q 마지막 요약 줄 → {passed, failed, errors, skipped}."""
    res = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for n, word in re.findall(r"(\d+) (passed|failed|errors?|skipped)", out):
        res["errors" if word.startswith("error") else word] = int(n)
    return res


def parse_verify(out: str) -> dict | None:
    m = re.search(r"(\d+)개 Item 중 불일치 (\d+)개", out)
    return {"items": int(m.group(1)), "mismatches": int(m.group(2))} if m else None


def run_pytest(repo: Path, python: str) -> dict:
    p = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=repo, capture_output=True, text=True, env=ENV)
    return parse_pytest(p.stdout + p.stderr) | {"returncode": p.returncode}


def run_verify(repo: Path, python: str, top_k: int) -> dict:
    p = subprocess.run([python, "scripts/verify_graph_search.py", "--top-k", str(top_k)], cwd=repo, capture_output=True, text=True, env=ENV)
    parsed = parse_verify(p.stdout)
    if parsed is None:
        return {"error": (p.stderr or p.stdout).strip()[-300:], "returncode": p.returncode}
    return parsed | {"returncode": p.returncode}


def graph_counts() -> dict:
    """읽기 전용 count. 접속 정보는 trend_agent.config.neo4j_settings(.env)."""
    sys.path.insert(0, str(ROOT / "src"))
    from neo4j import GraphDatabase, RoutingControl
    from trend_agent.config import neo4j_settings

    s = neo4j_settings()
    with GraphDatabase.driver(s.uri, auth=(s.user, s.password)) as driver:
        return {lb: driver.execute_query(f"MATCH (n:`{lb}`) RETURN count(n) AS c", routing_=RoutingControl.READ).records[0]["c"]
                for lb in LABELS}


def compare(measured: dict, base: dict) -> list[tuple[str, str, str, bool]]:
    """→ [(점검, 기준선, 측정, 통과)]"""
    rows = []
    if "pytest" in measured:
        m, b = measured["pytest"], base.get("pytest", {})
        rows.append(("pytest 실패·오류", "0", f"{m['failed']}·{m['errors']}", m["failed"] == 0 and m["errors"] == 0))
        rows.append(("pytest 통과 수(참고)", str(b.get("passed", "—")), str(m["passed"]), True))
    if "verify" in measured:
        m, b = measured["verify"], base.get("verify_graph_search", {})
        if "error" in m:
            rows.append(("verify_graph_search", "실행", "실패: " + m["error"], False))
        else:
            rows.append(("verify 불일치", "0", str(m["mismatches"]), m["mismatches"] == 0))
            rows.append(("verify Item 수", str(b.get("items", "—")), str(m["items"]), b.get("items") in (None, m["items"])))
    if "graph" in measured:
        for lb in LABELS:
            bv, mv = base.get("graph", {}).get(lb), measured["graph"].get(lb)
            rows.append((f"그래프 {lb}", str(bv if bv is not None else "—"), str(mv), bv is None or bv == mv))
    return rows


def render(rows, meta: dict) -> str:
    L = [f"# 회귀 점검 {meta['ts']}", "", f"- repo `{meta['repo']}` · git `{meta['git']}` · 기준선 {meta['baseline_updated']} ({meta['baseline_reason']})", "",
         "| 점검 | 기준선 | 측정 | 결과 |", "|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {'✅' if ok else '❌'} |" for a, b, c, ok in rows]
    return "\n".join(L) + "\n"


def update_baseline(path: Path, base: dict, measured: dict, reason: str, git: str) -> dict:
    today = dt.date.today().isoformat()
    hist = base.get("history", [])
    if base.get("updated_at"):
        hist.append({k: base.get(k) for k in ("updated_at", "reason", "graph", "verify_graph_search", "pytest")})
    new = {"version": 1, "updated_at": today, "reason": reason, "measured_at_git": git,
           "graph": measured.get("graph", base.get("graph")),
           "verify_graph_search": ({"top_k": measured.get("top_k", 5), "items": measured["verify"]["items"], "mismatches": measured["verify"]["mismatches"]}
                                   if "verify" in measured and "error" not in measured["verify"] else base.get("verify_graph_search")),
           "pytest": ({"passed": measured["pytest"]["passed"]} if "pytest" in measured else base.get("pytest")),
           "history": hist}
    path.write_text("# 회귀 기준선. 갱신은 eval_regression.py --update-baseline --reason \"…\" 로만 하고 커밋한다.\n"
                    + yaml.safe_dump(new, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return new


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--python", default=PYTHON)
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--skip-neo4j", action="store_true", help="verify_graph_search와 그래프 수를 건너뛴다")
    ap.add_argument("--skip-pytest", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--reason", help="--update-baseline 사유 (필수)")
    ap.add_argument("--out", type=Path, help="report.md를 쓸 경로 (기본: 화면만)")
    a = ap.parse_args(argv)
    if a.update_baseline and not a.reason:
        ap.error("--update-baseline에는 --reason이 필요합니다")

    base = yaml.safe_load(a.baseline.read_text(encoding="utf-8")) if a.baseline.exists() else {}
    measured: dict = {"top_k": a.top_k}
    if not a.skip_pytest:
        measured["pytest"] = run_pytest(a.repo, a.python)
    if not a.skip_neo4j:
        try:
            measured["graph"] = graph_counts()
        except Exception as e:  # 접속·인증 실패 — 비밀번호는 메시지에 없다
            print(f"Neo4j 읽기 실패: {type(e).__name__} {str(e)[:200]}", file=sys.stderr)
            return 2
        measured["verify"] = run_verify(a.repo, a.python, a.top_k)
    git = subprocess.run(["git", "-C", str(a.repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    if a.update_baseline:
        base = update_baseline(a.baseline, base, measured, a.reason, git)
        print(f"기준선 갱신: {a.baseline} ({a.reason})")
    rows = compare(measured, base)
    meta = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "repo": str(a.repo), "git": git,
            "baseline_updated": base.get("updated_at", "—"), "baseline_reason": base.get("reason", "—")}
    report = render(rows, meta)
    print(report)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(report, encoding="utf-8")
    return 0 if all(ok for *_, ok in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
