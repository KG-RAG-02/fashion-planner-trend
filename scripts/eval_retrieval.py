"""검색 평가: 후보 풀링 → (C3 판정) → 지표 · 사용자 표본 검수. 결과는 건수와 nDCG/P@5(Item 평균)로 보고한다.

하위 명령 (레포 루트, 공유 venv):
  pool          설정(eval/retrieval/configs/*.yaml)별 상위 depth를 Item마다 합집합 → <out>/pool.jsonl · items.jsonl · run.json
                Item·focus는 그래프에서 읽기 전용으로, Product는 data/processed(정규화 파일) 전수로 점수 계산(verify_graph_search B 경로와 같다)
  count         풀 규모(쌍 수·시트 수) — C3 일정 산정용
  metrics       judgments.jsonl로 설정별 nDCG@5·P@5·커버리지·이미지·snap/catalog·성별@5 + baseline 대비 Item별 증감·부트스트랩 구간
  review-sample 등급별 표본 + null 전부 → C3 등급을 숨긴 자가 완결 HTML(사용자 검수)
  review-score  사용자 등급 JSON vs C3 → 일치 건수·혼동표·가중 κ

점수 함수는 운영 코드(trend_agent.retrieval.scoring.rank_products·ScoringOptions)를 import한다. T3 브랜치가 develop에 들어오기 전에는
  --src "<T3 worktree>/src" 로 그 코드를 앞에 붙인다(읽기만 한다).
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import html
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "eval" / "retrieval" / "configs"
WEIGHTS = ROOT / "configs" / "weights.yaml"
MANIFEST = ROOT / "eval" / "datasets" / "article_extraction" / "manifest.yaml"
PROC = ROOT / "data" / "processed"
SHARED = Path(os.environ.get("SHARED", ROOT.parent / "_shared"))
SEED = 20261108
GRADES = (0, 1, 2)


def _use_src(src: str | None) -> None:
    if src:
        sys.path.insert(0, str(Path(src).resolve()))
    sys.path.insert(1 if src else 0, str(ROOT / "src"))


# ---------- 설정 ----------
def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_configs(paths: list[Path]) -> list[dict]:
    cfgs = []
    for p in paths:
        c = yaml.safe_load(p.read_text(encoding="utf-8"))
        c.setdefault("overrides", {})
        c.setdefault("depth", 20)
        cfgs.append(c)
    names = [c["name"] for c in cfgs]
    if len(set(names)) != len(names):
        raise SystemExit(f"설정 이름이 겹침: {names}")
    return cfgs


def weights_dict(path: Path = WEIGHTS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def scoring_for(cfg: dict, base: dict):
    """→ (WeightsConfig, ScoringOptions). overrides는 weights.yaml 구조 그대로 덮어쓴다."""
    from trend_agent.retrieval.scoring import ScoringOptions
    from trend_agent.retrieval.weights import WeightsConfig

    w = WeightsConfig.model_validate(deep_merge(base, cfg.get("overrides", {})))
    return w, ScoringOptions.from_weights(w)


# ---------- 지표 (순수 함수) ----------
def dcg(grades: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ndcg_at(ranked: list, judged: dict, k: int = 5) -> float | None:
    """ranked = product id 순서, judged = {product_id: grade(0/1/2/None)}. 미판정·null은 gain 0.
    IDCG는 그 Item의 판정된 풀 전체에서 구한다. IDCG = 0이면 None(= '관련 후보 없음')."""
    ideal = sorted((g for g in judged.values() if g is not None), reverse=True)[:k]
    idcg = dcg(ideal)
    if idcg == 0:
        return None
    return dcg([judged.get(p) or 0 for p in ranked[:k]]) / idcg


def gender_verdict(item_gender, product_gender) -> str:
    if product_gender is None:
        return "미상"
    return "일치" if product_gender in (item_gender, "공용") else "불일치"


def item_metrics(ranked: list, judged: dict, products: dict, item_gender, k: int = 5) -> dict:
    top = ranked[:k]
    g = [judged.get(p, "미판정") for p in top]
    m = {"n": len(top), "ndcg": ndcg_at(ranked, judged, k),
         "rel2": sum(1 for x in g if x == 2), "rel1p": sum(1 for x in g if x in (1, 2)),
         "unjudged": sum(1 for x in g if x == "미판정"), "null": sum(1 for x in g if x is None),
         "image": sum(1 for p in top if products.get(p, {}).get("has_image")),
         "snap": sum(1 for p in top if products.get(p, {}).get("source") == "musinsa_snap"),
         "catalog": sum(1 for p in top if products.get(p, {}).get("source") == "otpensource")}
    if item_gender in ("남", "여"):
        c = Counter(gender_verdict(item_gender, products.get(p, {}).get("gender")) for p in top)
        m.update({"gender_" + k_: c[k_] for k_ in ("일치", "불일치", "미상")})
    return m


def bootstrap_ci(deltas: list[float], n: int = 2000, seed: int = SEED, alpha: float = 0.05) -> tuple[float, float] | None:
    if not deltas:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(deltas) for _ in deltas) / len(deltas) for _ in range(n))
    return means[int(n * alpha / 2)], means[int(n * (1 - alpha / 2)) - 1]


def rankings_from_pool(pool_rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """pool.jsonl → {config: {item_id: [product_id 순위순]}}"""
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in pool_rows:
        for h in r["hits"]:
            out[h["config"]][r["item_id"]].append((h["rank"], r["product_id"]))
    return {c: {i: [p for _, p in sorted(v)] for i, v in items.items()} for c, items in out.items()}


def item_group(item_id: str, split_of: dict) -> str:
    if item_id.startswith(("virtual", "t4:")):
        return "Tier4 프로필"
    aid = item_id.split("-s")[0]
    return {"dev": "dev Style", "test": "test Style"}.get(split_of.get(aid), "기타")


def aggregate(per_item: dict[str, dict]) -> dict:
    vals = [m["ndcg"] for m in per_item.values() if m["ndcg"] is not None]
    agg = {"items": len(per_item), "ndcg_items": len(vals), "no_relevant": sum(1 for m in per_item.values() if m["ndcg"] is None),
           "ndcg_mean": round(sum(vals) / len(vals), 4) if vals else None,
           "empty_lt5": sum(1 for m in per_item.values() if m["n"] < 5)}
    for k in ("n", "rel2", "rel1p", "unjudged", "null", "image", "snap", "catalog", "gender_일치", "gender_불일치", "gender_미상"):
        agg[k] = sum(m.get(k, 0) for m in per_item.values())
    return agg


def compute_metrics(rankings: dict, judgments: dict, products: dict, items: dict, split_of: dict, baseline: str, k: int = 5) -> dict:
    """→ {config: {group: aggregate, 'vs_baseline': {...}}}"""
    out = {}
    base_rank = rankings.get(baseline, {})
    for cfg, by_item in rankings.items():
        per_group: dict[str, dict] = defaultdict(dict)
        per_item_all = {}
        for iid in items:
            m = item_metrics(by_item.get(iid, []), judgments.get(iid, {}), products, items[iid].get("gender"), k)
            per_group[item_group(iid, split_of)][iid] = m
            per_item_all[iid] = m
        res = {g: aggregate(v) for g, v in sorted(per_group.items())}
        res["전체"] = aggregate(per_item_all)
        if cfg != baseline and base_rank:
            better = worse = same = 0
            deltas = []
            for iid in items:
                a = ndcg_at(by_item.get(iid, []), judgments.get(iid, {}), k)
                b = ndcg_at(base_rank.get(iid, []), judgments.get(iid, {}), k)
                if a is None or b is None:
                    continue
                d = a - b
                deltas.append(d)
                better += d > 1e-9
                worse += d < -1e-9
                same += abs(d) <= 1e-9
            ci = bootstrap_ci(deltas)
            res["vs_baseline"] = {"좋아짐": better, "나빠짐": worse, "같음": same,
                                  "평균 차이": round(sum(deltas) / len(deltas), 4) if deltas else None,
                                  "95% 구간": [round(x, 4) for x in ci] if ci else None}
        out[cfg] = res
    return out


# ---------- 판정 입출력 ----------
def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def judgments_map(rows: list[dict], pass_no: int = 1) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(dict)
    for r in rows:
        if r.get("pass", 1) == pass_no:
            out[r["item_id"]][r["product_id"]] = r["grade"]
    return out


def weighted_kappa(pairs: list[tuple[int, int]], cats=GRADES) -> float | None:
    """2차 가중 κ (등급 0/1/2)."""
    if not pairs:
        return None
    n, K = len(pairs), len(cats)
    idx = {c: i for i, c in enumerate(cats)}
    obs = [[0] * K for _ in range(K)]
    for a, b in pairs:
        obs[idx[a]][idx[b]] += 1
    ra = [sum(row) for row in obs]
    cb = [sum(obs[i][j] for i in range(K)) for j in range(K)]
    w = lambda i, j: (i - j) ** 2 / (K - 1) ** 2
    num = sum(w(i, j) * obs[i][j] for i in range(K) for j in range(K))
    den = sum(w(i, j) * ra[i] * cb[j] / n for i in range(K) for j in range(K))
    return 1 - num / den if den else None


def review_sample(rows: list[dict], per_grade: int = 10, seed: int = SEED) -> list[dict]:
    """등급별 per_grade쌍 + null 전부. 등급 순서가 드러나지 않게 섞는다."""
    rng = random.Random(seed)
    by = defaultdict(list)
    for r in rows:
        if r.get("pass", 1) == 1:
            by[r["grade"]].append(r)
    pick = []
    for g in GRADES:
        lst = sorted(by[g], key=lambda r: (r["item_id"], r["product_id"]))
        pick += rng.sample(lst, min(per_grade, len(lst)))
    pick += sorted(by[None], key=lambda r: (r["item_id"], r["product_id"]))
    rng.shuffle(pick)
    return pick


def _img_b64(path: str | None, max_side: int = 360) -> str | None:
    if not path or not Path(path).exists():
        return None
    from io import BytesIO

    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def review_html(sample: list[dict], items: dict, products: dict) -> str:
    """C3 등급을 숨긴 자가 완결 HTML. '결과 복사' → [{item_id, product_id, grade, note}] JSON."""
    cards = []
    for n, r in enumerate(sample, 1):
        it, p = items.get(r["item_id"], {}), products.get(r["product_id"], {})
        b64 = _img_b64(p.get("local_path"))
        img = f'<img src="data:image/jpeg;base64,{b64}">' if b64 else '<div class="noimg">이미지 없음</div>'
        radios = "".join(f'<label><input type="radio" name="g{n}" value="{g}"> {g}</label>' for g in ("2", "1", "0", "null"))
        cards.append(f'''<section class="pair" data-item="{html.escape(r["item_id"])}" data-product="{html.escape(r["product_id"])}" data-n="{n}">
<h3>{n}. {html.escape(it.get("style_name", ""))} — {html.escape(it.get("name", ""))}</h3>
<p class="desc">{html.escape(it.get("header", ""))}</p>{img}
<p class="prod">{html.escape(p.get("card_text", ""))}</p>
<div class="radios">{radios}</div><input class="note" placeholder="사유(선택)"></section>''')
    return f'''<!DOCTYPE html><html lang="ko"><meta charset="utf-8"><title>검색 판정 표본 검수</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:900px;margin:auto;padding:1em}}.pair{{border:1px solid #ccc;border-radius:8px;padding:1em;margin:1em 0}}
img{{max-width:360px;max-height:360px;display:block}}.noimg{{width:200px;height:120px;background:#eee;display:flex;align-items:center;justify-content:center}}
.note{{width:100%;margin-top:.5em}}textarea{{width:100%;height:10em}}</style>
<h1>검색 판정 표본 검수 ({len(sample)}쌍)</h1>
<p>각 쌍에서 상품이 위 Item의 예로 적절한지 고르세요. 2 = 관련(같은 종류, 핵심 속성 확인), 1 = 부분 관련(같은 상분류, 일부만 맞음), 0 = 무관, null = 이미지로 판단 불가.
판정자(C3)의 등급은 숨겨져 있습니다. 다 고르면 아래 '결과 복사'를 눌러 T4 세션에 붙여 넣어 주세요.</p>
{"".join(cards)}
<button id="copy">결과 복사</button><textarea id="out" readonly></textarea>
<script>
document.getElementById("copy").addEventListener("click", function () {{
  var res = [];
  document.querySelectorAll(".pair").forEach(function (s) {{
    var c = s.querySelector("input[type=radio]:checked");
    res.push({{item_id: s.dataset.item, product_id: s.dataset.product, grade: c ? (c.value === "null" ? null : Number(c.value)) : "미입력",
              note: s.querySelector(".note").value}});
  }});
  var t = JSON.stringify(res);
  var out = document.getElementById("out"); out.value = t; out.select();
  if (navigator.clipboard) {{ navigator.clipboard.writeText(t); }}
}});
</script></html>
'''


def review_score(user_rows: list[dict], c3_rows: list[dict]) -> dict:
    c3 = {(r["item_id"], r["product_id"]): r["grade"] for r in c3_rows if r.get("pass", 1) == 1}
    conf = Counter()
    pairs = []
    for u in user_rows:
        key = (u["item_id"], u["product_id"])
        if key not in c3 or u["grade"] == "미입력":
            continue
        conf[(str(c3[key]), str(u["grade"]))] += 1
        if c3[key] in GRADES and u["grade"] in GRADES:
            pairs.append((c3[key], u["grade"]))
    agree = sum(n for (a, b), n in conf.items() if a == b)
    k = weighted_kappa(pairs)
    return {"쌍": sum(conf.values()), "일치": agree, "불일치": sum(conf.values()) - agree,
            "혼동표(C3→사용자)": {f"{a}→{b}": n for (a, b), n in sorted(conf.items())},
            "가중 κ(0/1/2 쌍)": round(k, 4) if k is not None else None, "κ 쌍 수": len(pairs)}


# ---------- 풀링 (그래프·정규화 파일) ----------
def product_info(p) -> dict:
    n = p.node
    return {"source": n.source, "gender": getattr(n.gender, "value", n.gender), "has_image": n.has_image,
            "snap_id": n.snap_id, "name": n.name, "category_major": getattr(p.category_major, "value", p.category_major),
            "category_minor": getattr(n.category_minor, "value", n.category_minor),
            "content_sha": hashlib.sha1(p.model_dump_json().encode("utf-8")).hexdigest()[:12]}


def read_products_by_major(proc: Path = PROC):
    from trend_agent.graph.loader import read_products

    by_major: dict[str, list] = defaultdict(list)
    for name in ("otpensource_all.jsonl", "musinsa_snap_all.jsonl"):
        for r in read_products(proc / name):
            if r.in_graph_scope:
                by_major[r.category_major.value].append(r)
    return by_major


def read_graph_items():
    """→ [{item: ItemRecord, style_id, style_name, core, sup}] (읽기 전용)."""
    from neo4j import GraphDatabase, RoutingControl

    from trend_agent.config import neo4j_settings
    from trend_agent.graph.search import ITEMS_CYPHER, _record
    from trend_agent.schemas.graph_schema import ItemNode, ItemRecord

    s = neo4j_settings()
    out = []
    with GraphDatabase.driver(s.uri, auth=(s.user, s.password)) as driver:
        styles = [dict(r["s"]) for r in driver.execute_query("MATCH (s:Style) RETURN s ORDER BY s.id", routing_=RoutingControl.READ).records]
        for st in styles:
            for r in driver.execute_query(ITEMS_CYPHER, style_ids=[st["id"]], routing_=RoutingControl.READ).records:
                out.append({"item": _record(ItemRecord, ItemNode, r), "style_id": st["id"], "style_name": st.get("name"),
                            "core": list(st.get("focus_core") or []), "sup": list(st.get("focus_supporting") or [])})
    return out


def filter_entries(entries: list[dict], split_of: dict, splits: set[str]) -> list[dict]:
    """라벨 Item만(기사 id의 manifest split ∈ splits). 자동 추출 Item은 판정하지 않는다(비용 — 설계 메모 §5.1)."""
    return [e for e in entries if split_of.get(e["item"].node.id.split("-s")[0]) in splits]


def item_info(e: dict) -> dict:
    it = e["item"]
    n = it.node
    attrs = {a: sorted(v) for a in ("color", "material", "pattern", "detail", "fit") if (v := _attr_values(it, a))}
    return {"item_id": n.id, "name": n.name, "style_id": e["style_id"], "style_name": e["style_name"],
            "category_major": getattr(it.category_major, "value", it.category_major),
            "category_minor": getattr(n.category_minor, "value", n.category_minor),
            "gender": getattr(n.gender, "value", n.gender), "raw_text": getattr(n, "raw_text", None),
            "attrs": attrs, "focus_core": e["core"], "focus_supporting": e["sup"]}


def _attr_values(rec, attr):
    from trend_agent.retrieval.scoring import attribute_values

    return attribute_values(rec, attr)


def build_pool(entries: list[dict], by_major: dict, configs: list[dict], base_weights: dict):
    from trend_agent.retrieval.scoring import rank_products

    sc = {c["name"]: scoring_for(c, base_weights) for c in configs}
    pool: dict[tuple, list] = defaultdict(list)
    products: dict[str, dict] = {}
    for e in entries:
        it = e["item"]
        cands = by_major.get(getattr(it.category_major, "value", None), [])
        for c in configs:
            w, opts = sc[c["name"]]
            for rank, s in enumerate(rank_products(it, cands, w, e["core"], e["sup"], top_k=c["depth"], options=opts), 1):
                pid = s.product.node.id
                pool[(it.node.id, pid)].append({"config": c["name"], "rank": rank, "score": round(s.score, 6)})
                products.setdefault(pid, product_info(s.product))
    rows = [{"item_id": i, "product_id": p, "hits": h} for (i, p), h in sorted(pool.items())]
    return rows, products


# ---------- CLI ----------
def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cmd_pool(a) -> int:
    configs = load_configs(sorted(a.configs) if a.configs else sorted(CONFIG_DIR.glob("*.yaml")))
    base = weights_dict(a.weights)
    entries = filter_entries(read_graph_items(), _split_of(), set(a.splits.split(",")))
    by_major = read_products_by_major(a.proc)
    rows, products = build_pool(entries, by_major, configs, base)
    items = [item_info(e) for e in entries]
    out = a.out
    _write_jsonl(out / "pool.jsonl", rows)
    _write_jsonl(out / "items.jsonl", items)
    _write_jsonl(out / "products.jsonl", [{"product_id": k, **v} for k, v in sorted(products.items())])
    run = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "configs": configs, "weights": str(a.weights),
           "weights_sha": hashlib.sha256(json.dumps(base, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12],
           "proc": str(a.proc), "src": a.src, "splits": a.splits, "items": len(items), "pairs": len(rows), "products": len(products)}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: run[k] for k in ("items", "pairs", "products")}, ensure_ascii=False), "→", out)
    return 0


def tier4_entries(result: dict) -> tuple[dict, list[str]] | None:
    """run_trend_pipeline 결과 JSON(강제 Tier4) → (items.jsonl 행, 트렌드 Top-K product id 순서). 프로필이 없으면 None.
    판정 키 = 가상 Style id + context_sha 앞 8자(T3: 프롬프트·맥락이 바뀌면 같은 id가 다른 프로필일 수 있다)."""
    if not result.get("trends"):
        return None
    t, r = result["trends"][0], result["resolutions"][0]
    llm = r.get("llm") or {}
    iid = f"{t['style']['id']}#{(llm.get('context_sha') or '')[:8]}"
    attrs: dict[str, set] = defaultdict(set)
    for it in t["items"]:
        for a, key in (("color", "colors"), ("material", "materials"), ("pattern", "patterns")):
            attrs[a] |= {v if isinstance(v, str) else v.get("value") for v in it.get(key, [])}
        attrs["detail"] |= {f"{d.get('type')}:{d.get('name') or d.get('value')}" for d in it.get("details", [])}
        if it.get("fit"):
            attrs["fit"].add(it["fit"])
    cats = sorted({f"{it.get('category_major')}>{it.get('category_minor') or '-'}" for it in t["items"]})
    row = {"item_id": iid, "name": f"Tier4 프로필 ({len(t['items'])} Item: {', '.join(cats)})", "style_id": t["style"]["id"],
           "style_name": r.get("term") or t["style"]["name"], "category_major": "/".join(sorted({c.split('>')[0] for c in cats})),
           "category_minor": "/".join(sorted({c.split('>')[1] for c in cats})), "gender": None, "raw_text": t["style"].get("definition"),
           "attrs": {a: sorted(v for v in vs if v) for a, vs in attrs.items() if any(vs)},
           "focus_core": t["style"].get("focus_core", []), "focus_supporting": t["style"].get("focus_supporting", []),
           "tier4": {"prompt_version": llm.get("prompt_version"), "context_sha": llm.get("context_sha"), "evidence": llm.get("evidence"),
                     "tier4_context": result.get("applied_settings", {}).get("tier4_context"),
                     "hidden": result.get("applied_settings", {}).get("hidden_style_groups")}}
    return row, [p["product_id"] for p in r.get("top_products", [])]


def cmd_pool_tier4(a) -> int:
    """C단계: Tier4 결과 JSON들의 트렌드 Top-K를 판정 풀로 (config 'T4', rank = 결과 순서)."""
    by_id = {p.node.id: p for ps in read_products_by_major(a.proc).values() for p in ps}
    items, rows, products = [], [], {}
    for f in sorted(p for d in a.runs for p in d.glob("*.json")):
        got = tier4_entries(json.loads(f.read_text(encoding="utf-8")))
        if got is None:
            continue
        row, pids = got
        row["result_file"] = str(f)
        items.append(row)
        for rank, pid in enumerate(pids, 1):
            rows.append({"item_id": row["item_id"], "product_id": pid, "hits": [{"config": "T4", "rank": rank, "score": None}]})
            if pid in by_id:
                products.setdefault(pid, product_info(by_id[pid]))
    missing = sorted({r["product_id"] for r in rows} - products.keys())
    _write_jsonl(a.out / "pool.jsonl", rows)
    _write_jsonl(a.out / "items.jsonl", items)
    _write_jsonl(a.out / "products.jsonl", [{"product_id": k, **v} for k, v in sorted(products.items())])
    (a.out / "run.json").write_text(json.dumps({"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "runs": [str(d) for d in a.runs],
                                                "items": len(items), "pairs": len(rows), "missing_products": missing}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"items": len(items), "pairs": len(rows), "missing_products": len(missing)}, ensure_ascii=False), "→", a.out)
    return 0 if not missing else 1


def cmd_count(a) -> int:
    rows = read_jsonl(a.pool / "pool.jsonl")
    per_item = Counter(r["item_id"] for r in rows)
    sheets = sum(math.ceil(n / a.per_sheet) for n in per_item.values())
    print(json.dumps({"items": len(per_item), "pairs": len(rows), "sheets": sheets, "max_per_item": max(per_item.values(), default=0)}, ensure_ascii=False))
    return 0


def _split_of() -> dict:
    return {x["article_id"]: x.get("split") for x in yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["articles"]}


def cmd_metrics(a) -> int:
    rows = read_jsonl(a.pool / "pool.jsonl")
    items = {r["item_id"]: r for r in read_jsonl(a.pool / "items.jsonl")}
    products = {r["product_id"]: r for r in read_jsonl(a.pool / "products.jsonl")}
    res = compute_metrics(rankings_from_pool(rows), judgments_map(read_jsonl(a.judgments)), products, items, _split_of(), a.baseline)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "metrics.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    L = ["# 검색 지표 (설정별)", "", f"- 풀 `{a.pool}` · 판정 `{a.judgments}` · baseline `{a.baseline}` · k=5", "",
         "| 설정 | 그룹 | Item | nDCG@5 평균(관련 있는 Item) | 관련 후보 없음 | P@5 (g=2 / g≥1, 분모 5n) | 미판정@5 | null@5 | 이미지@5 | snap/catalog@5 | 성별@5 일치·불일치·미상 | <5개 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cfg, groups in res.items():
        for g, m in groups.items():
            if g == "vs_baseline":
                continue
            L.append(f"| {cfg} | {g} | {m['items']} | {m['ndcg_mean']} ({m['ndcg_items']}) | {m['no_relevant']} | {m['rel2']} / {m['rel1p']} / {5 * m['items']} | "
                     f"{m['unjudged']} | {m['null']} | {m['image']} | {m['snap']}/{m['catalog']} | {m['gender_일치']}·{m['gender_불일치']}·{m['gender_미상']} | {m['empty_lt5']} |")
    L += ["", "| 설정 | baseline 대비 Item별 nDCG@5 좋아짐·나빠짐·같음 | 평균 차이 | 부트스트랩 95% 구간(Item 재표집 2,000회) |", "|---|---|---|---|"]
    for cfg, groups in res.items():
        if "vs_baseline" in groups:
            v = groups["vs_baseline"]
            L.append(f"| {cfg} | {v['좋아짐']}·{v['나빠짐']}·{v['같음']} | {v['평균 차이']} | {v['95% 구간']} |")
    (a.out / "metrics.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("wrote", a.out / "metrics.md")
    return 0


def cmd_review_sample(a) -> int:
    rows = read_jsonl(a.judgments)
    items = {r["item_id"]: r for r in read_jsonl(a.pool / "items.jsonl")}
    products = {r["product_id"]: r for r in read_jsonl(a.pool / "products.jsonl")}
    sheets = {(r["item_id"], c["product_id"]): c for r in read_jsonl(a.sheets) for c in r.get("cards", [])} if a.sheets else {}
    for k, c in sheets.items():  # 시트에 쓴 카드 문구·이미지 경로를 그대로 쓴다
        products.setdefault(k[1], {}).update({"card_text": c.get("card_text", ""), "local_path": c.get("local_path")})
    for i in items.values():
        i.setdefault("header", i.get("header_text", ""))
    sample = review_sample(rows, a.per_grade, a.seed)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(review_html(sample, items, products), encoding="utf-8")
    print(f"wrote {a.out} ({len(sample)}쌍)")
    return 0


def cmd_review_score(a) -> int:
    res = review_score(json.loads(a.user.read_text(encoding="utf-8")), read_jsonl(a.judgments))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", help="앞에 붙일 src 경로 (T3 코드가 develop에 들어오기 전)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pool")
    p.add_argument("--configs", type=Path, nargs="*")
    p.add_argument("--splits", default="dev,test", help="풀에 넣을 라벨 split (기본 dev,test — 자동 추출 Item 제외)")
    p.add_argument("--weights", type=Path, default=WEIGHTS)
    p.add_argument("--proc", type=Path, default=PROC)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("pool-tier4", help="C단계: Tier4 결과 JSON 폴더들 → 판정 풀")
    p.add_argument("--runs", type=Path, nargs="+", required=True)
    p.add_argument("--proc", type=Path, default=PROC)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("count")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--per-sheet", type=int, default=20)
    p = sub.add_parser("metrics")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--judgments", type=Path, default=SHARED / "eval" / "judgments.jsonl")
    p.add_argument("--baseline", default="P0")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("review-sample")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--judgments", type=Path, default=SHARED / "eval" / "judgments.jsonl")
    p.add_argument("--sheets", type=Path, help="build_contact_sheets의 sheets.jsonl (카드 문구·이미지)")
    p.add_argument("--per-grade", type=int, default=10)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--out", type=Path, default=SHARED / "eval" / "review" / "sample.html")
    p = sub.add_parser("review-score")
    p.add_argument("--user", type=Path, required=True)
    p.add_argument("--judgments", type=Path, default=SHARED / "eval" / "judgments.jsonl")
    a = ap.parse_args(argv)
    _use_src(a.src)
    return {"pool": cmd_pool, "pool-tier4": cmd_pool_tier4, "count": cmd_count, "metrics": cmd_metrics, "review-sample": cmd_review_sample,
            "review-score": cmd_review_score}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
