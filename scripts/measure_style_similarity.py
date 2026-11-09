"""Style 임베딩 유사도 측정 (Style 해소 B2의 사전 확인). 로컬 BGE-M3, 외부 호출 없음.

실행 (레포 루트, .venv, torch·sentence-transformers 필요):
    HF_HUB_OFFLINE=1 python scripts/measure_style_similarity.py [--runs <실험 폴더> ...]

A. 이름 + 정의 (D-12 입력 형식 "<이름>: <정의>", 이름만도 함께): 정답 라벨과 실험 결과의 Style을 모은다.
   - 같은 트렌드 쌍 = 같은 기사에서 라벨 Style과 이름이 맞는 Style(느슨한 이름 키, 평가용)
   - 다른 트렌드 쌍 = 서로 다른 기사의 그런 Style
   두 분포가 얼마나 떨어져 있는지(같은 쌍 최솟값 vs 다른 쌍 최댓값)를 본다.
B. 이름만, manifest 트렌드명 전체 대상: 실험 Style 이름마다 가장 가까운 manifest 이름 5개.
   같은 기사의 이름이 1위인지, 2위(다른 기사)와 얼마나 차이 나는지를 본다. manifest에는 정의가 없어 이름만 비교한다.
출력: data/processed/experiments/embedding_<YYYYMMDD-HHMMSS>/ (similarity.md · pairs.json · record.json). git 제외.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import yaml  # noqa: E402

from compare_extraction import _norm  # noqa: E402  평가용 느슨한 이름 키 (그래프 이름 일치에는 쓰지 않는다)
from trend_agent.embeddings import embed_texts  # noqa: E402
from trend_agent.embeddings.runtime import DEFAULT_MODEL, _query_encoder  # noqa: E402

KST = timezone(timedelta(hours=9))
EXP = ROOT / "data" / "processed" / "experiments"
LABELS = ROOT / "eval" / "datasets" / "article_extraction" / "labels"
MANIFEST = ROOT / "eval" / "datasets" / "article_extraction" / "manifest.yaml"
TEXT_FORMAT = "{name}: {definition}"


def styles_from_doc(doc: dict) -> list[dict]:
    ext = doc.get("extraction") or {}
    return [{"name": s.get("name") or "", "definition": s.get("definition") or ""} for s in ext.get("styles") or []]


def collect(runs: list[Path]) -> list[dict]:
    """[{source, article_id, name, definition, main}] — main = 같은 기사 라벨 Style과 이름이 맞음."""
    label_names: dict[str, list[str]] = {}
    out: list[dict] = []
    for p in sorted(LABELS.glob("*.json")):
        aid = p.stem
        for s in styles_from_doc(json.loads(p.read_text(encoding="utf-8"))):
            label_names.setdefault(aid, []).append(s["name"])
            out.append({"source": "label", "article_id": aid, **s, "main": True})
    for run in runs:
        for f in sorted(run.glob("v*_*.json")) + sorted((run / "_review").glob("v*_*.json")):
            doc = json.loads(f.read_text(encoding="utf-8"))
            if "label_meta" not in doc:
                continue
            aid = str(doc["label_meta"]["article_id"])
            keys = {_norm(n) for n in label_names.get(aid, [])}
            for s in styles_from_doc(doc):
                k = _norm(s["name"])
                main = any(k == g or k in g or g in k for g in keys if k and g)
                out.append({"source": run.name, "article_id": aid, **s, "main": main})
    seen, uniq = set(), []
    for r in out:  # 같은 (기사, 이름, 정의)는 한 번만
        key = (r["article_id"], r["name"], r["definition"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def _stats(xs: list[float]) -> dict:
    return {"n": len(xs), "min": round(min(xs), 3), "median": round(statistics.median(xs), 3), "max": round(max(xs), 3)} if xs else {"n": 0}


def part_a(recs: list[dict]) -> dict:
    mains = [r for r in recs if r["main"]]
    others = [r for r in recs if not r["main"]]
    res: dict = {"records": len(recs), "main": len(mains), "not_main": len(others)}
    for mode in ("name+definition", "name"):
        texts = [TEXT_FORMAT.format(**r) if mode == "name+definition" else r["name"] for r in mains]
        v = embed_texts(texts)
        same, diff = [], []
        for i, j in itertools.combinations(range(len(mains)), 2):
            sim = float(v[i] @ v[j])
            row = {"a": f"{mains[i]['article_id']}:{mains[i]['name']}", "b": f"{mains[j]['article_id']}:{mains[j]['name']}",
                   "sim": round(sim, 3), "a_src": mains[i]["source"], "b_src": mains[j]["source"]}
            (same if mains[i]["article_id"] == mains[j]["article_id"] else diff).append(row)
        res[mode] = {"same_trend": _stats([r["sim"] for r in same]), "different_trend": _stats([r["sim"] for r in diff]),
                     "lowest_same": sorted(same, key=lambda r: r["sim"])[:5],
                     "highest_different": sorted(diff, key=lambda r: -r["sim"])[:5], "pairs_same": same}
    # 같은 기사 안에서 라벨과 이름이 안 맞는 Style(예: Item을 Style로 쪼갠 것) ↔ 그 기사 라벨
    if others:
        labels = {r["article_id"]: r for r in recs if r["source"] == "label"}
        texts = [TEXT_FORMAT.format(**r) for r in others] + [TEXT_FORMAT.format(**labels[r["article_id"]]) for r in others]
        v = embed_texts(texts)
        n = len(others)
        res["not_main_vs_label"] = [{"article_id": r["article_id"], "name": r["name"], "source": r["source"],
                                     "label": labels[r["article_id"]]["name"], "sim": round(float(v[k] @ v[n + k]), 3)}
                                    for k, r in enumerate(others)]
    return res


def part_b(recs: list[dict]) -> list[dict]:
    man = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["articles"]
    names = [(str(a["article_id"]), s) for a in man for s in (a.get("styles") or [])]
    queries = sorted({(r["article_id"], r["name"]) for r in recs if r["source"] != "label"})
    vq = embed_texts([q for _, q in queries])
    vm = embed_texts([n for _, n in names])
    sims = vq @ vm.T
    out = []
    for qi, (aid, q) in enumerate(queries):
        order = sims[qi].argsort()[::-1]
        top = [{"article_id": names[k][0], "name": names[k][1], "sim": round(float(sims[qi][k]), 3),
                "same_article": names[k][0] == aid} for k in order[:5]]
        own = [float(sims[qi][k]) for k in range(len(names)) if names[k][0] == aid]
        best_other = max(float(sims[qi][k]) for k in range(len(names)) if names[k][0] != aid)
        out.append({"article_id": aid, "query": q, "own_best": round(max(own), 3) if own else None,
                    "best_other": round(best_other, 3), "margin": round(max(own) - best_other, 3) if own else None,
                    "own_rank1": bool(top and top[0]["same_article"]), "top5": top})
    return out


def write_md(path: Path, rec: dict, a: dict, b: list[dict]) -> None:
    L = [f"# Style 임베딩 유사도 측정 `{rec['run_id']}`", "",
         f"- 모델 `{rec['model']}` (revision `{rec['model_revision'][:12]}`), 로컬·오프라인 · 텍스트 형식 `{TEXT_FORMAT}` · "
         f"코드 `scripts/measure_style_similarity.py` (sha256 `{rec['script_sha256'][:12]}`, git `{rec['git_head'][:10]}`)",
         f"- 입력: 정답 라벨 + 실험 {len(rec['runs'])}개 폴더의 Style {a['records']}개 (라벨과 이름이 맞는 Style {a['main']}개, 안 맞는 {a['not_main']}개)",
         "- 결과는 건수가 적어(기사 5건) 방향 확인용이다.", "", "## A. 같은 트렌드 vs 다른 트렌드", "",
         "| 입력 | 같은 트렌드 쌍 (최소 / 중앙 / 최대) | 다른 트렌드 쌍 (최소 / 중앙 / 최대) | 여유 (같은 최소 − 다른 최대) |", "|---|---|---|---|"]
    for mode in ("name+definition", "name"):
        s, d = a[mode]["same_trend"], a[mode]["different_trend"]
        L.append(f"| {mode} | {s.get('min')} / {s.get('median')} / {s.get('max')} (n={s['n']}) | "
                 f"{d.get('min')} / {d.get('median')} / {d.get('max')} (n={d['n']}) | {round(s['min'] - d['max'], 3)} |")
    for mode in ("name+definition", "name"):
        L += ["", f"**{mode}: 가장 낮은 같은 트렌드 쌍**", ""] + [f"- {r['sim']} · {r['a']} ↔ {r['b']}" for r in a[mode]["lowest_same"]]
        L += ["", f"**{mode}: 가장 높은 다른 트렌드 쌍**", ""] + [f"- {r['sim']} · {r['a']} ↔ {r['b']}" for r in a[mode]["highest_different"]]
    if a.get("not_main_vs_label"):
        L += ["", "**라벨과 이름이 다른 Style ↔ 그 기사 라벨 Style (name+definition)**", ""]
        L += [f"- {r['sim']} · {r['article_id']}:{r['name']} ↔ {r['label']} ({r['source']})" for r in a["not_main_vs_label"]]
    L += ["", "## B. 이름만 — manifest 트렌드명 대상 최근접", "",
          "| 기사 | 실험 Style 이름 | 같은 기사 이름 최고 | 다른 기사 최고 | 여유 | 1위가 같은 기사 | 상위 5 |", "|---|---|---|---|---|---|---|"]
    for r in b:
        top = "; ".join(f"{t['name']}({t['sim']}{'*' if t['same_article'] else ''})" for t in r["top5"])
        L.append(f"| {r['article_id']} | {r['query']} | {r['own_best']} | {r['best_other']} | {r['margin']} | "
                 f"{'예' if r['own_rank1'] else '아니오'} | {top} |")
    L += ["", "`*` = 같은 기사의 manifest 이름"]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", nargs="*", type=Path, default=None, help="실험 폴더 (기본: experiments/의 모든 실험)")
    a = ap.parse_args(argv)
    runs = [r.resolve() for r in a.runs] if a.runs else sorted(p.parent for p in EXP.glob("*/v*_EXPERIMENT.json"))
    started = datetime.now(KST)
    out = EXP / f"embedding_{started:%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    recs = collect(runs)
    res_a, res_b = part_a(recs), part_b(recs)
    enc = _query_encoder(DEFAULT_MODEL, None)
    script = Path(__file__).resolve()
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    rec = {"run_id": out.name, "started_at": started.isoformat(timespec="seconds"), "model": DEFAULT_MODEL,
           "model_revision": enc.revision, "device": enc.device, "text_format": TEXT_FORMAT,
           "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "git_head": git,
           "runs": [str(r.relative_to(ROOT)).replace("\\", "/") for r in runs], "n_styles": len(recs)}
    (out / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "pairs.json").write_text(json.dumps({"styles": recs, "A": res_a, "B": res_b}, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    write_md(out / "similarity.md", rec, res_a, res_b)
    print(out / "similarity.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
