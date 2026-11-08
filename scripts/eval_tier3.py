"""Tier3(의미 일치) τ/δ 보정: 보정 세트(eval/datasets/tier3_calibration/pairs.yaml) × T3 tier3_candidates 점수 → 격자별 TA·FA·TR·FR 건수.

실행 (레포 루트, 공유 venv, Neo4j 읽기 + C1 로컬 벡터):
  python scripts/eval_tier3.py score --out "$SHARED/eval/tier3/<run>"            # 질의마다 후보 그룹·점수(candidates.jsonl)
  python scripts/eval_tier3.py calibrate --cands "$SHARED/eval/tier3/<run>/candidates.jsonl" --out eval/results/tier3/<run>

판정(운영 Resolver와 같다): 1위 그룹 점수 best ≥ τ 이고 best − second(다른 그룹 1위) ≥ δ 이면 수용.
  expected가 있으면  수용 ∧ 1위 ∈ expected → TA · 수용 ∧ 1위 ∉ expected → FA(오수용) · 거절 → FR(오거절)
  expected가 []이면  수용 → FA · 거절 → TR
선택: confusable·absent·unrelated의 FA = 0 → (전체 FA 최소) → TA 최대 → 실현 가능 영역 안에서 경계까지 여유가 가장 큰 점.
쌍을 시드로 반반 나눠 선택용(A)으로 고르고 확인용(B)에서 같은 점의 건수를 함께 낸다(표본이 작아 방향 확인용).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PAIRS = ROOT / "eval" / "datasets" / "tier3_calibration" / "pairs.yaml"
SEED = 20261108
TAUS = [round(0.30 + 0.01 * i, 2) for i in range(66)]          # 0.30 … 0.95
DELTAS = [round(0.005 * i, 3) for i in range(41)]               # 0 … 0.20
NEG_KINDS = ("confusable", "absent", "unrelated")


def norm(s: str) -> str:
    from trend_agent.schemas.graph_schema import normalize_term

    return normalize_term(s)


def decide(cands: list[dict], tau: float, delta: float) -> str | None:
    """후보(점수 내림차순, 그룹 단위) → 수용한 그룹 norm 또는 None."""
    if not cands:
        return None
    best = cands[0]["score"]
    second = cands[1]["score"] if len(cands) > 1 else 0.0
    return cands[0]["group"] if best >= tau and best - second >= delta else None


def outcome(expected: list[str], accepted: str | None) -> str:
    exp = {norm(e) for e in expected}
    if exp:
        return "FR" if accepted is None else "TA" if accepted in exp else "FA"
    return "TR" if accepted is None else "FA"


def grid_counts(rows: list[dict], taus=TAUS, deltas=DELTAS) -> dict[tuple, dict]:
    """rows = [{query, expected, kind, cands}] → {(τ, δ): {TA, FA, TR, FR, FA_neg}}"""
    out = {}
    for t in taus:
        for d in deltas:
            c = {"TA": 0, "FA": 0, "TR": 0, "FR": 0, "FA_neg": 0}
            for r in rows:
                o = outcome(r["expected"], decide(r["cands"], t, d))
                c[o] += 1
                c["FA_neg"] += o == "FA" and r["kind"] in NEG_KINDS
            out[(t, d)] = c
    return out


def select(counts: dict[tuple, dict]) -> tuple | None:
    feas = {k: v for k, v in counts.items() if v["FA_neg"] == 0}
    if not feas:
        return None
    min_fa = min(v["FA"] for v in feas.values())
    feas = {k: v for k, v in feas.items() if v["FA"] == min_fa}
    max_ta = max(v["TA"] for v in feas.values())
    best = [k for k, v in feas.items() if v["TA"] == max_ta]
    # 여유: 같은 (FA_neg=0, FA=min, TA=max) 조건을 만족하는 이웃 격자점 수(상하좌우 반경) — 가장자리 대신 안쪽 점
    keys = set(best)
    ti = {t: i for i, t in enumerate(sorted({k[0] for k in counts}))}
    di = {d: i for i, d in enumerate(sorted({k[1] for k in counts}))}

    def margin(k):
        r = 0
        while True:
            r += 1
            ring = [(t, d) for (t, d) in counts if max(abs(ti[t] - ti[k[0]]), abs(di[d] - di[k[1]])) == r]
            if not ring or any(x not in keys for x in ring):
                return r - 1

    return max(best, key=lambda k: (margin(k), -k[0], -k[1]))


def split_halves(rows: list[dict], seed: int = SEED) -> tuple[list, list]:
    idx = list(range(len(rows)))
    random.Random(seed).shuffle(idx)
    h = len(idx) // 2
    return [rows[i] for i in sorted(idx[:h])], [rows[i] for i in sorted(idx[h:])]


def cmd_score(a) -> int:
    from neo4j import GraphDatabase

    from trend_agent.config import neo4j_settings
    from trend_agent.graph.search import TrendSearcher
    from trend_agent.resolver.resolver import TrendResolver

    pairs = yaml.safe_load(a.pairs.read_text(encoding="utf-8"))["pairs"]
    s = neo4j_settings()
    a.out.mkdir(parents=True, exist_ok=True)
    with GraphDatabase.driver(s.uri, auth=(s.user, s.password)) as driver, (a.out / "candidates.jsonl").open("w", encoding="utf-8") as f:
        res = TrendResolver(TrendSearcher(driver))
        for p in pairs:
            cands = [{"group": c.group_norm, "score": round(float(c.score), 6), "style_ids": c.style_ids} for c in res.tier3_candidates(p["query"], k=a.k)]
            f.write(json.dumps(p | {"cands": cands}, ensure_ascii=False) + "\n")
    print("wrote", a.out / "candidates.jsonl", len(pairs))
    return 0


def cmd_calibrate(a) -> int:
    rows = [json.loads(l) for l in a.cands.read_text(encoding="utf-8").splitlines() if l.strip()]
    full, (A, B) = grid_counts(rows), split_halves(rows)
    ca, cb = grid_counts(A), grid_counts(B)
    pick, pick_a = select(full), select(ca)
    a.out.mkdir(parents=True, exist_ok=True)
    L = ["# Tier3 τ/δ 보정 (건수)", "", f"- 보정 세트 {len(rows)}쌍 · 격자 τ {TAUS[0]}–{TAUS[-1]}(0.01) × δ {DELTAS[0]}–{DELTAS[-1]}(0.005)",
         "- 규칙: confusable·absent·unrelated 오수용 0 → 전체 오수용 최소 → 정답 수용 최대 → 여유가 큰 점", ""]
    if pick:
        c = full[pick]
        L.append(f"- **전체 세트 선택: τ = {pick[0]}, δ = {pick[1]}** → TA {c['TA']} · FA {c['FA']} · TR {c['TR']} · FR {c['FR']}")
    else:
        L.append("- 전체 세트에서 오수용 0인 점이 없다 → Tier3를 켜지 않는다(τ/δ null 유지) 권장")
    if pick_a:
        L.append(f"- 반반 검증: A({len(A)}쌍)에서 고른 τ = {pick_a[0]}, δ = {pick_a[1]} → A {ca[pick_a]} / B({len(B)}쌍) {cb[pick_a]}")
    L += ["", "| 질의 | 종류 | 정답 | 1위 그룹(점수) | 2위 그룹(점수) | 선택 점 결과 |", "|---|---|---|---|---|---|"]
    for r in rows:
        c0 = r["cands"][0] if r["cands"] else {"group": "—", "score": 0}
        c1 = r["cands"][1] if len(r["cands"]) > 1 else {"group": "—", "score": 0}
        o = outcome(r["expected"], decide(r["cands"], *pick)) if pick else "—"
        L.append(f"| {r['query']} | {r['kind']} | {', '.join(r['expected']) or '(거절)'} | {c0['group']} ({c0['score']}) | {c1['group']} ({c1['score']}) | {o} |")
    (a.out / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (a.out / "summary.json").write_text(json.dumps({"pick": pick, "pick_counts": full.get(pick), "pick_A": pick_a,
                                                    "A": ca.get(pick_a), "B": cb.get(pick_a), "n": len(rows)}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", a.out / "report.md")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("score")
    p.add_argument("--pairs", type=Path, default=PAIRS)
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("calibrate")
    p.add_argument("--cands", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    return {"score": cmd_score, "calibrate": cmd_calibrate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
