"""Tier4 숨김 평가: 라벨 Style을 숨기고 강제 Tier4(맥락 없음)로 만든 가상 프로필을, 같은 Style의 라벨 Item 기반 결과와 비교한다(건수).

선행 (Neo4j 읽기만, LLM 호출은 Style마다 1회 — term_map 캐시는 T4 폴더에 둔다):
  python scripts/run_trend_pipeline.py --trend <이름> --hide-style <style id> --force-tier 4 --tier4-context none \
         --top-k 20 --terms "$SHARED/eval/tier4_hidden/<run>/term_map.jsonl" --out "$SHARED/eval/tier4_hidden/<run>" --request-id hidden-<style id>
실행:
  python scripts/eval_tier4_hidden.py --runs "$SHARED/eval/tier4_hidden/<run>" --out eval/results/tier4_hidden/<run>

지표(정답 = 숨긴 Style의 라벨 Item, 같은 weights·flags, trend_topk K=20):
- 인식: Tier4가 프로필을 냈는가(recognized) 또는 llm_unrecognized
- 겹침@5·@10·@20: Tier4 트렌드 Top-K 상품 id ∩ 정답 트렌드 Top-K 상품 id 건수
- focus (속성, level): 일치·level 다름·누락·오추출 / 속성 값(하분류·색·소재·패턴·디테일·핏): 일치·누락·오추출 건수
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import eval_retrieval as ER  # noqa: E402

VALUE_ATTRS = ("category_minor", "color", "material", "pattern", "detail", "fit")


def tier4_values(items: list[dict]) -> dict[str, set]:
    """결과 JSON의 가상 Item들 → 속성별 값 합집합 (detail은 '<type>:<name>')."""
    out = {a: set() for a in VALUE_ATTRS}
    for it in items:
        if it.get("category_minor"):
            out["category_minor"].add(it["category_minor"])
        for a, key in (("color", "colors"), ("material", "materials"), ("pattern", "patterns")):
            out[a] |= {v if isinstance(v, str) else v.get("value") for v in it.get(key, [])}
        out["detail"] |= {f"{d.get('type')}:{d.get('name') or d.get('value')}" for d in it.get("details", [])}
        if it.get("fit"):
            out["fit"].add(it["fit"])
    return {a: {v for v in s if v} for a, s in out.items()}


def compare_values(t4: dict[str, set], gold: dict[str, set]) -> Counter:
    c = Counter()
    for a in VALUE_ATTRS:
        c[f"{a}|일치"] += len(t4[a] & gold[a])
        c[f"{a}|누락"] += len(gold[a] - t4[a])
        c[f"{a}|오추출"] += len(t4[a] - gold[a])
    return c


def compare_focus(t4_core, t4_sup, g_core, g_sup) -> Counter:
    t = {a: "core" for a in t4_core} | {a: "supporting" for a in t4_sup}
    g = {a: "core" for a in g_core} | {a: "supporting" for a in g_sup}
    c = Counter()
    for a in t.keys() | g.keys():
        c["누락" if a not in t else "오추출" if a not in g else "일치" if t[a] == g[a] else "level 다름"] += 1
    return c


def overlaps(t4_ids: list[str], gold_ids: list[str], ks=(5, 10, 20)) -> dict[int, int]:
    return {k: len(set(t4_ids[:k]) & set(gold_ids[:k])) for k in ks}


def _pid(pick) -> str:
    for path in (("product", "product_id"), ("product_id",), ("product", "id")):
        x = pick
        try:
            for p in path:
                x = getattr(x, p)
            return x
        except AttributeError:
            continue
    raise AttributeError("TrendPick에서 product id를 찾지 못함")


def gold_for(style_id: str, entries: list[dict], by_major: dict, k: int = 20):
    """정답: 그 Style의 라벨 Item × weights.yaml(P0) rank_products(pool_per_item) → trend_topk."""
    from trend_agent.retrieval.scoring import ScoringOptions, attribute_values, rank_products
    from trend_agent.retrieval.trend_topk import ItemRanking, trend_topk
    from trend_agent.retrieval.weights import load_weights

    w = load_weights()
    opts = ScoringOptions.from_weights(w)
    mine = [e for e in entries if e["style_id"] == style_id]
    rankings, vals = [], {a: set() for a in VALUE_ATTRS}
    for e in mine:
        it = e["item"]
        ranked = rank_products(it, by_major.get(it.category_major.value, []), w, e["core"], e["sup"], top_k=w.trend_topk.pool_per_item, options=opts)
        rankings.append(ItemRanking(it.node.id, it.node.name, 1.0, ranked))
        if it.node.category_minor is not None:
            vals["category_minor"].add(getattr(it.node.category_minor, "value", it.node.category_minor))
        for a in ("color", "material", "pattern", "detail", "fit"):
            vals[a] |= attribute_values(it, a)
    picks = trend_topk(rankings, k, image_first=opts.image_first_tiebreak)
    core, sup = (mine[0]["core"], mine[0]["sup"]) if mine else ([], [])
    return [_pid(p) for p in picks], vals, core, sup, len(mine)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    entries = ER.read_graph_items()
    by_major = ER.read_products_by_major()
    rows, total = [], Counter()
    for f in sorted(a.runs.glob("hidden-*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        style_id = f.stem.removeprefix("hidden-")
        res = d["resolutions"][0]
        gold_ids, gvals, gcore, gsup, n_items = gold_for(style_id, entries, by_major)
        row = {"style_id": style_id, "term": res["term"], "status": res["status"], "tier": res.get("tier"),
               "steps": [s["code"] for s in res.get("steps", [])], "llm": res.get("llm"), "gold_items": n_items}
        if d.get("trends"):
            t = d["trends"][0]
            t4_ids = [p["product_id"] for p in res.get("top_products", [])]
            row |= {"recognized": True, "overlap": overlaps(t4_ids, gold_ids), "t4_items": len(t["items"]),
                    "focus": dict(compare_focus(t["style"]["focus_core"], t["style"]["focus_supporting"], gcore, gsup)),
                    "values": dict(compare_values(tier4_values(t["items"]), gvals)),
                    "t4_focus": [t["style"]["focus_core"], t["style"]["focus_supporting"]], "gold_focus": [gcore, gsup]}
            total["인식"] += 1
            for k, v in row["overlap"].items():
                total[f"겹침@{k}"] += v
            total.update({f"focus|{k}": v for k, v in row["focus"].items()})
            total.update({f"값 {k}": v for k, v in row["values"].items()})
        else:
            row["recognized"] = False
            total["미인식"] += 1
        rows.append(row)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "summary.json").write_text(json.dumps({"rows": rows, "total": dict(total)}, ensure_ascii=False, indent=1), encoding="utf-8")
    L = ["# Tier4 숨김 평가 (건수)", "", f"- 결과 폴더 `{a.runs}` · 정답 = 숨긴 Style의 라벨 Item(weights.yaml 기본) trend_topk K=20 · Tier4 맥락 none", "",
         "| Style | 용어 | 결과 | Tier4 Item | 정답 Item | 겹침@5 | @10 | @20 | focus 일치·level 다름·누락·오추출 | Tier4 focus (core / sup) | 정답 focus |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["recognized"]:
            f = r["focus"]
            L.append(f"| {r['style_id']} | {r['term']} | 프로필 | {r['t4_items']} | {r['gold_items']} | {r['overlap'][5]} | {r['overlap'][10]} | {r['overlap'][20]} | "
                     f"{f.get('일치', 0)}·{f.get('level 다름', 0)}·{f.get('누락', 0)}·{f.get('오추출', 0)} | {r['t4_focus'][0]} / {r['t4_focus'][1]} | {r['gold_focus'][0]} / {r['gold_focus'][1]} |")
        else:
            L.append(f"| {r['style_id']} | {r['term']} | **미인식**({r['steps'][-1] if r['steps'] else r['status']}) | 0 | {r['gold_items']} | — | — | — | — | — | — |")
    L += ["", "## 속성 값 (Tier4 Item 합집합 vs 정답 Item 합집합, 인식된 Style 합계)", "", "| 속성 | 일치 | 누락 | 오추출 |", "|---|---|---|---|"]
    L += [f"| {at} | {total[f'값 {at}|일치']} | {total[f'값 {at}|누락']} | {total[f'값 {at}|오추출']} |" for at in VALUE_ATTRS]
    (a.out / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("wrote", a.out / "report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
