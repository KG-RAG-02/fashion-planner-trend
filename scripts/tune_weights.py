"""가중치·플래그 격자 탐색: (Item, Product) 성분을 1회 계산하고 설정별 점수를 numpy로 계산해 판정 기반 nDCG@5로 고른다.

실행 (레포 루트, 공유 venv):
  python scripts/tune_weights.py check  [--configs 20] [--top-k 20]                 # 운영 rank_products와 동치 확인(필수 선행)
  python scripts/tune_weights.py search --pool "$SHARED/eval/pool/<run>" [--grid eval/retrieval/grid.yaml] \
         [--judgments "$SHARED/eval/judgments.jsonl"] --out "$SHARED/eval/tune/<run>"

점수 식(운영 trend_agent.retrieval.scoring과 같다):
  W_a = base_a × 배수(core/supporting/default), base = Σ W_a·c_a·r_a ÷ (Σ W_a·c_a + γ·Σ_{a∈core} W_a·u_a), score = base + m·α·배수(category)
  c = 비교 가능(Item·Product 둘 다 값), r = |공통|/|Item 값|, u = Item 값 있음 ∧ Product 미확인, m = 하분류 일치.
  정렬 = (−round(score, 9), −비교 속성 수, −일치 값 수, [−has_image], id) · 점수 0 제외 · [성별 필터] 먼저 · [snap_id 제한] 정렬 뒤.
선택: 커버리지@5 ≥ 0.9인 설정 중 평균 nDCG@5 최대, 제약 = 이미지@5 ≥ baseline · 성별 불일치@5 ≤ baseline.
      Style 단위 leave-one-out으로 선택 절차의 일반화와 파라미터 선택 빈도를 낸다. 상위 설정의 미판정 쌍은 2차 풀로 낸다.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import eval_retrieval as ER  # noqa: E402

ATTRS = ("color", "material", "pattern", "detail", "fit")
GRID = ROOT / "eval" / "retrieval" / "grid.yaml"
SEED = 20261108


# ---------- 성분 ----------
@dataclass
class Components:
    """한 Item의 후보(같은 상분류 Product 전부) 성분. 배열 길이 = 후보 수."""
    item_id: str
    style_id: str
    core: frozenset
    sup: frozenset
    gender: str | None
    ids: np.ndarray        # str
    comp: np.ndarray       # (N, 5) bool  비교 가능
    ratio: np.ndarray      # (N, 5) float 일치 비율 (비교 가능할 때만 의미)
    unknown: np.ndarray    # (N, 5) bool  Item 값 있음 ∧ Product 미확인
    nmatch: np.ndarray     # (N,) int    공통 값 수 (sort_key의 len(matched))
    minor: np.ndarray      # (N,) bool   하분류 일치
    image: np.ndarray      # (N,) bool
    snap: np.ndarray       # (N,) int    snap_id, 없으면 -1
    gender_ok: np.ndarray  # (N,) bool   gender_compatible


def components(entry: dict, products: list) -> Components:
    from trend_agent.retrieval.scoring import attribute_values, category_minor_of, gender_compatible

    it = entry["item"]
    iv = {a: attribute_values(it, a) for a in ATTRS}
    im = category_minor_of(it)
    n = len(products)
    comp, ratio, unk = np.zeros((n, 5), bool), np.zeros((n, 5)), np.zeros((n, 5), bool)
    nmatch, minor = np.zeros(n, int), np.zeros(n, bool)
    image, snap, gok = np.zeros(n, bool), np.full(n, -1), np.zeros(n, bool)
    ids = []
    for j, p in enumerate(products):
        ids.append(p.node.id)
        for k, a in enumerate(ATTRS):
            if not iv[a]:
                continue
            pv = attribute_values(p, a)
            if not pv:
                unk[j, k] = True
                continue
            common = iv[a] & pv
            comp[j, k] = True
            ratio[j, k] = len(common) / len(iv[a])
            nmatch[j] += len(common)
        minor[j] = im is not None and im == category_minor_of(p)
        image[j] = p.node.has_image
        snap[j] = p.node.snap_id if p.node.snap_id is not None else -1
        gok[j] = gender_compatible(it, p)
    g = getattr(it.node.gender, "value", it.node.gender)
    return Components(it.node.id, entry["style_id"], frozenset(entry["core"]), frozenset(entry["sup"]), g, np.array(ids, dtype=object),
                      comp, ratio, unk, nmatch, minor, image, snap, gok)


# ---------- 설정 → 순위 ----------
@dataclass(frozen=True)
class Setting:
    base: tuple            # ATTRS 순서의 base weight
    alpha: float
    m_core: float
    m_sup: float
    gamma: float = 0.0
    image_first: bool = False
    snap_limit: int = 0
    gender: bool = False
    m_default: float = 1.0

    def as_dict(self) -> dict:
        return {"base": dict(zip(ATTRS, self.base)), "alpha": self.alpha, "focus": [self.m_core, self.m_sup], "gamma": self.gamma,
                "image_first": self.image_first, "snap_limit": self.snap_limit, "gender": self.gender}


def setting_from_weights(w: dict) -> Setting:
    f, fm = w.get("flags", {}), w.get("focus_multipliers", {})
    return Setting(tuple(float(w["base_weights"][a]) for a in ATTRS), float(w["category"]["minor_bonus_alpha"]),
                   float(fm.get("core", 1.0)), float(fm.get("supporting", 1.0)), float(f.get("core_unknown_decay", 0.0)),
                   bool(f.get("image_first_tiebreak", False)), int(f.get("snap_id_limit", 0)), bool(f.get("item_gender_filter", False)),
                   float(fm.get("default", 1.0)))


def weights_from_setting(s: Setting, base: dict) -> dict:
    return ER.deep_merge(base, {"base_weights": dict(zip(ATTRS, s.base)), "category": {"minor_bonus_alpha": s.alpha},
                                "focus_multipliers": {"core": s.m_core, "supporting": s.m_sup, "default": s.m_default},
                                "flags": {"core_unknown_decay": s.gamma, "image_first_tiebreak": s.image_first,
                                          "snap_id_limit": s.snap_limit, "item_gender_filter": s.gender}})


def _mult(attr: str, c: Components, s: Setting) -> float:
    return s.m_core if attr in c.core else s.m_sup if attr in c.sup else s.m_default


def scores(c: Components, s: Setting) -> np.ndarray:
    W = np.array([s.base[k] * _mult(a, c, s) for k, a in enumerate(ATTRS)])
    num = (c.comp * c.ratio) @ W
    den = c.comp @ W
    core_mask = np.array([a in c.core for a in ATTRS])
    unk = (c.unknown & core_mask) @ W * s.gamma if s.gamma else np.zeros(len(num))
    denom = den + unk
    base = np.divide(num, denom, out=np.zeros_like(num), where=denom > 0)
    return base + c.minor * (s.alpha * _mult("category", c, s))


def rank(c: Components, s: Setting, top_k: int | None = None) -> list[tuple[str, float]]:
    sc = scores(c, s)
    keep = sc > 0.0
    if s.gender:
        keep &= c.gender_ok
    idx = np.nonzero(keep)[0]
    if not len(idx):
        return []
    r = np.round(sc[idx], 9)
    keys = [c.ids[idx], -c.image[idx].astype(int) if s.image_first else np.zeros(len(idx), int), -c.nmatch[idx], -c.comp[idx].sum(1), -r]
    order = idx[np.lexsort(keys)]  # 마지막 키가 1순위
    out, seen = [], Counter()
    for j in order:
        if s.snap_limit and c.snap[j] >= 0:
            seen[c.snap[j]] += 1
            if seen[c.snap[j]] > s.snap_limit:
                continue
        out.append((c.ids[j], float(sc[j])))
        if top_k and len(out) >= top_k:
            break
    return out


# ---------- 격자 ----------
def load_grid(path: Path = GRID) -> list[Setting]:
    g = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    for b, a, (mc, ms), img, gam, snap, gen in itertools.product(
            g["base_weights"].values(), g["alpha"], g["focus"], g["image_first"], g["gamma"], g["snap_limit"], g["gender"]):
        out.append(Setting(tuple(float(b[x]) for x in ATTRS), float(a), float(mc), float(ms), float(gam), bool(img), int(snap), bool(gen)))
    return out


def evaluate(comps: list[Components], s: Setting, judg: dict, products: dict, k: int = 5) -> dict:
    per_item = {}
    for c in comps:
        top = [p for p, _ in rank(c, s, k)]
        per_item[c.item_id] = ER.item_metrics(top, judg.get(c.item_id, {}), products, c.gender, k)
    return per_item


def summarize(per_item: dict) -> dict:
    a = ER.aggregate(per_item)
    n = a["n"]
    a["coverage"] = round(1 - a["unjudged"] / n, 4) if n else 0.0
    return a


def select(rows: list[dict], base_row: dict, min_cov: float = 0.9):
    ok = [r for r in rows if r["m"]["coverage"] >= min_cov and r["m"]["ndcg_mean"] is not None
          and r["m"]["image"] >= base_row["m"]["image"] and r["m"].get("gender_불일치", 0) <= base_row["m"].get("gender_불일치", 0)]
    return sorted(ok, key=lambda r: (-r["m"]["ndcg_mean"], json.dumps(r["setting"], sort_keys=True)))


def loo(comps: list[Components], settings: list[Setting], judg: dict, products: dict, base: Setting) -> dict:
    """Style 단위 leave-one-out: 나머지 Style로 고른 설정을 빠진 Style에서 평가."""
    cache = {s: evaluate(comps, s, judg, products) for s in settings}
    styles = sorted({c.style_id for c in comps})
    chosen, held = Counter(), []
    for st in styles:
        train = [c.item_id for c in comps if c.style_id != st]
        test = [c.item_id for c in comps if c.style_id == st]
        rows = [{"setting": s.as_dict(), "s": s, "m": summarize({i: cache[s][i] for i in train})} for s in settings]
        brow = {"m": summarize({i: cache[base][i] for i in train})}
        best = select(rows, brow)
        if not best:
            continue
        b = best[0]["s"]
        chosen[json.dumps(b.as_dict(), sort_keys=True)] += 1
        t_best, t_base = summarize({i: cache[b][i] for i in test}), summarize({i: cache[base][i] for i in test})
        held.append({"style": st, "chosen": b.as_dict(), "ndcg_chosen": t_best["ndcg_mean"], "ndcg_base": t_base["ndcg_mean"]})
    return {"held_out": held, "choice_frequency": chosen.most_common(10)}


# ---------- CLI ----------
def _load(proc: Path, splits: str = "dev,test"):
    entries = ER.filter_entries(ER.read_graph_items(), ER._split_of(), set(splits.split(",")))
    by_major = ER.read_products_by_major(proc)
    return entries, by_major


def cmd_check(a) -> int:
    """운영 rank_products와 상위 top_k id·점수 완전 일치 확인: baseline(weights.yaml) + 무작위 설정."""
    from trend_agent.retrieval.scoring import ScoringOptions, rank_products
    from trend_agent.retrieval.weights import WeightsConfig

    base = ER.weights_dict()
    entries, by_major = _load(a.proc, a.splits)
    settings = [setting_from_weights(base)]
    grid = load_grid(a.grid)
    settings += random.Random(SEED).sample(grid, min(a.configs, len(grid)))
    bad = total = 0
    for s in settings:
        w = WeightsConfig.model_validate(weights_from_setting(s, base))
        opts = ScoringOptions.from_weights(w)
        for e in entries[:: a.stride]:
            prods = by_major.get(e["item"].category_major.value, [])
            ref = [(x.product.node.id, round(x.score, 9)) for x in rank_products(e["item"], prods, w, e["core"], e["sup"], top_k=a.top_k, options=opts)]
            got = [(p, round(v, 9)) for p, v in rank(components(e, prods), s, a.top_k)]
            total += 1
            if ref != got:
                bad += 1
                print("DIFF", s.as_dict(), e["item"].node.id, ref[:3], got[:3])
    print(f"동치 확인: 설정 {len(settings)} × Item {len(entries[:: a.stride])} = {total}건 중 불일치 {bad}건")
    return 1 if bad else 0


def cmd_search(a) -> int:
    base_w = ER.weights_dict()
    base = setting_from_weights(base_w)
    entries, by_major = _load(a.proc, a.splits)
    comps = [components(e, by_major.get(e["item"].category_major.value, [])) for e in entries]
    products = {r["product_id"]: r for r in ER.read_jsonl(a.pool / "products.jsonl")}
    # 지표용 product 정보(이미지·출처·성별): 풀 밖 상품이 상위에 오면 여기에 없다 → 그래프 레코드로 채운다
    for e in entries:
        for p in by_major.get(e["item"].category_major.value, []):
            if p.node.id not in products:
                products[p.node.id] = {"source": p.node.source, "gender": getattr(p.node.gender, "value", p.node.gender), "has_image": p.node.has_image}
    judg = ER.judgments_map(ER.read_jsonl(a.judgments))
    settings = load_grid(a.grid)
    if base not in settings:
        settings.append(base)
    rows = []
    for s in settings:
        rows.append({"setting": s.as_dict(), "s": s, "m": summarize(evaluate(comps, s, judg, products))})
    base_row = next(r for r in rows if r["s"] == base)
    best = select(rows, base_row)
    a.out.mkdir(parents=True, exist_ok=True)
    with (a.out / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"setting": r["setting"], "metrics": r["m"]}, ensure_ascii=False) + "\n")
    lo = loo(comps, [r["s"] for r in best[:a.loo_top]] + [base], judg, products, base) if best else {}
    # 2차 풀: 상위 설정 top5의 미판정 쌍
    second = sorted({(c.item_id, p) for r in best[:5] for c in comps for p, _ in rank(c, r["s"], 5) if p not in judg.get(c.item_id, {})})
    with (a.out / "pool2.jsonl").open("w", encoding="utf-8") as f:
        for i, p in second:
            f.write(json.dumps({"item_id": i, "product_id": p, "hits": [{"config": "tune-top5", "rank": 0, "score": 0}]}, ensure_ascii=False) + "\n")
    L = ["# 가중치 튜닝 보고 (건수·평균)", "", f"- 격자 {len(settings)}개 · Item {len(comps)} · 판정 `{a.judgments}` · 제약: 커버리지@5 ≥ 0.9, 이미지@5 ≥ baseline, 성별 불일치@5 ≤ baseline",
         f"- baseline {base.as_dict()}: nDCG@5 {base_row['m']['ndcg_mean']} · 커버리지 {base_row['m']['coverage']} · 이미지 {base_row['m']['image']} · 성별 불일치 {base_row['m'].get('gender_불일치', 0)}",
         f"- 제약을 만족한 설정 {len(best)}개 · 2차 풀(상위 5 설정의 미판정 쌍) {len(second)}쌍 → pool2.jsonl", "",
         "| 순위 | 설정 | nDCG@5 | 커버리지 | P@5 g=2 | 이미지@5 | 성별 불일치@5 |", "|---|---|---|---|---|---|---|"]
    for n, r in enumerate(best[:5], 1):
        m = r["m"]
        L.append(f"| {n} | `{json.dumps(r['setting'], ensure_ascii=False)}` | {m['ndcg_mean']} | {m['coverage']} | {m['rel2']} | {m['image']} | {m.get('gender_불일치', 0)} |")
    if lo:
        L += ["", "## Style 단위 leave-one-out", "", "| 빠진 Style | 고른 설정 nDCG@5 | baseline nDCG@5 |", "|---|---|---|"]
        L += [f"| {h['style']} | {h['ndcg_chosen']} | {h['ndcg_base']} |" for h in lo["held_out"]]
        L += ["", "선택 빈도(상위): " + "; ".join(f"{n}회 {k}" for k, n in lo["choice_frequency"])]
    (a.out / "tuning_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("wrote", a.out / "tuning_report.md")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("--configs", type=int, default=20)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--stride", type=int, default=1, help="Item을 몇 개 걸러 확인할지(1 = 전부)")
    p.add_argument("--grid", type=Path, default=GRID)
    p.add_argument("--proc", type=Path, default=ER.PROC)
    p.add_argument("--splits", default="dev,test", help="라벨 split만 (자동 추출 Item 제외)")
    p = sub.add_parser("search")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--grid", type=Path, default=GRID)
    p.add_argument("--judgments", type=Path, default=ER.SHARED / "eval" / "judgments.jsonl")
    p.add_argument("--splits", default="dev,test", help="라벨 split만 (자동 추출 Item 제외)")
    p.add_argument("--loo-top", type=int, default=50, help="LOO에서 다시 고를 후보 설정 수(상위)")
    p.add_argument("--proc", type=Path, default=ER.PROC)
    p.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    return {"check": cmd_check, "search": cmd_search}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
