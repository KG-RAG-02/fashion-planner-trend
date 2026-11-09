"""products v1 ↔ v2 전·후 수치표 (T2 작업 5 · 설계 메모 §3.5).

실행:  python scripts/audit_products.py --v1 이전_산출물_폴더 --v2 새_산출물_폴더 [--out DIR]
출력:  <out>/audit_v1_v2.md · <out>/audit_v1_v2.json (기본 out = v2 폴더)

비교: 건수·그래프 대상(상분류 있음)·상분류/하분류·속성 채움·approx·분해/팩/후보·dup_of·빈 행·노이즈 null·
키워드 오탐 제거(같은 id의 Pattern·Detail 엣지 차이)·색 별칭 효과·이미지 상태. 입력 JSONL만 읽는다(읽기 전용).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.normalization.output import resolve_out, write_json_atomic, write_text_atomic  # noqa: E402

FILES = {"otpensource": "otpensource_all.jsonl", "musinsa_snap": "musinsa_snap_all.jsonl"}
KO_BASIC_COLORS = {"보라", "보라색", "violet", "바이올렛", "빨강", "빨간색", "분홍", "분홍색", "주황", "주황색", "노랑", "노란색",
                   "초록", "초록색", "파랑", "파란색", "갈색"}


def load(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize(recs: list[dict[str, Any]]) -> dict[str, Any]:
    n = [r["node"] for r in recs]
    def cnt(pred) -> int:
        return sum(1 for r in recs if pred(r))
    return {
        "rows": len(recs),
        "in_graph_scope": cnt(lambda r: r.get("category_major")),
        "category_minor": cnt(lambda r: r["node"].get("category_minor")),
        "category_major_only": cnt(lambda r: r.get("category_major") and not r["node"].get("category_minor")),
        "category_null": cnt(lambda r: not r.get("category_major")),
        "by_category_major": dict(sorted(Counter(r["category_major"] for r in recs if r.get("category_major")).items())),
        "color_rows": cnt(lambda r: r.get("colors")),
        "color_approx_rows": cnt(lambda r: any(c.get("approx") for c in r.get("colors", []))),
        "material_rows": cnt(lambda r: r.get("materials")),
        "material_approx_rows": cnt(lambda r: any(c.get("approx") for c in r.get("materials", []))),
        "pattern_rows": cnt(lambda r: r.get("patterns")),
        "pattern_edges": sum(len(r.get("patterns", [])) for r in recs),
        "detail_rows": cnt(lambda r: r.get("details")),
        "detail_edges": sum(len(r.get("details", [])) for r in recs),
        "shape_rows": sum(1 for x in n if x.get("shape")),
        "fit_rows": sum(1 for x in n if x.get("fit")),
        "length_body_rows": sum(1 for x in n if x.get("length_body")),
        "length_sleeve_rows": sum(1 for x in n if x.get("length_sleeve")),
        "approx_scalar_rows": sum(1 for x in n if x.get("approx")),
        "sub_color_rows": sum(1 for x in n if x.get("sub_colors")),
        "split_components": sum(1 for x in n if x.get("parent_id")),
        "split_parents": len({x["parent_id"] for x in n if x.get("parent_id")}),
        "multipack": sum(1 for x in n if x.get("multipack")),
        "split_candidate": sum(1 for x in n if x.get("split_candidate")),
        "dup_of": sum(1 for x in n if x.get("dup_of")),
        "snap_id_rows": sum(1 for x in n if x.get("snap_id") is not None),
        "distinct_snap_ids": len({x["snap_id"] for x in n if x.get("snap_id") is not None}),
        "multi_wearer_suspected": sum(1 for x in n if x.get("multi_wearer_suspected")),
        "image_status": dict(sorted(Counter(str(x.get("image_status")) for x in n).items())),
        "image_scope": dict(sorted(Counter(str(x.get("image_scope")) for x in n).items())),
        "empty_name_url": sum(1 for x in n if not x.get("name") and not x.get("image_url") and x["source"] == "otpensource"),
    }


def edge_diff(v1: list[dict[str, Any]], v2: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """같은 id 상품에서 사라진·생긴 엣지 값(키워드 오탐 제거 효과)."""
    a = {r["node"]["id"]: r for r in v1}
    b = {r["node"]["id"]: r for r in v2}
    removed: Counter = Counter()
    added: Counter = Counter()
    removed_raw: Counter = Counter()
    for pid in a.keys() & b.keys():
        va = {(e.get("type"), e["value"]): e for e in a[pid].get(field, [])}
        vb = {(e.get("type"), e["value"]): e for e in b[pid].get(field, [])}
        for k in va.keys() - vb.keys():
            removed[k[1]] += 1
            removed_raw[f"{k[1]} ← {a[pid]['node'].get('name') or ''}"[:60]] += 1
        for k in vb.keys() - va.keys():
            added[k[1]] += 1
    return {"removed": dict(removed.most_common()), "added": dict(added.most_common()),
            "removed_examples": [k for k, _ in removed_raw.most_common(15)]}


def category_transitions(v1: list[dict[str, Any]], v2: list[dict[str, Any]]) -> dict[str, int]:
    a = {r["node"]["id"]: r for r in v1}
    out: Counter = Counter()
    for r in v2:
        pid = r["node"]["id"]
        if pid not in a:
            continue
        def state(x: dict[str, Any]) -> str:
            if not x.get("category_major"):
                return "null"
            return "minor" if x["node"].get("category_minor") else "major_only"
        s1, s2 = state(a[pid]), state(r)
        if s1 != s2:
            out[f"{s1}→{s2}"] += 1
        elif s1 == "minor" and a[pid]["node"]["category_minor"] != r["node"]["category_minor"]:
            out["minor_changed"] += 1
        elif s1 != "null" and a[pid]["category_major"] != r["category_major"]:
            out["major_changed"] += 1
    return dict(out.most_common())


def color_alias_effect(v2: list[dict[str, Any]]) -> dict[str, Any]:
    c: Counter = Counter()
    for r in v2:
        for e in r.get("colors", []):
            raw = (e.get("raw") or "").strip().lower()
            if raw in KO_BASIC_COLORS:
                c[f"{e.get('raw')}→{e['value']}"] += 1
        for s in r["node"].get("sub_colors", []):
            pass
    return {"edges_from_new_aliases": sum(c.values()), "by_raw": dict(c.most_common())}


def table(rows: list[tuple[str, Any, Any]]) -> str:
    out = ["| 항목 | v1 | v2 | 차이 |", "|---|---:|---:|---:|"]
    for k, a, b in rows:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out.append(f"| {k} | {a:,} | {b:,} | {b - a:+,} |")
        else:
            out.append(f"| {k} | {a} | {b} | |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--v1", required=True)
    ap.add_argument("--v2", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    out = resolve_out(a.out or a.v2)
    result: dict[str, Any] = {"v1": str(Path(a.v1).resolve()), "v2": str(Path(a.v2).resolve()), "sources": {}}
    md = ["# products v1 ↔ v2 감사표", "", f"- v1: `{a.v1}`", f"- v2: `{a.v2}`",
          "- 생성: `scripts/audit_products.py` (입력 JSONL만 읽음)", ""]
    for src, fname in FILES.items():
        v1 = load(Path(a.v1) / fname)
        v2 = load(Path(a.v2) / fname)
        s1, s2 = summarize(v1), summarize(v2)
        res = {"v1": s1, "v2": s2,
               "category_transitions": category_transitions(v1, v2),
               "pattern_edge_diff": edge_diff(v1, v2, "patterns"),
               "detail_edge_diff": edge_diff(v1, v2, "details"),
               "material_edge_diff": edge_diff(v1, v2, "materials"),
               "color_edge_diff": edge_diff(v1, v2, "colors"),
               "color_alias_effect": color_alias_effect(v2)}
        result["sources"][src] = res
        keys = [k for k in s1 if not isinstance(s1[k], dict)]
        md += [f"## {src}", "", table([(k, s1[k], s2[k]) for k in keys]), ""]
        for k in ("by_category_major", "image_status", "image_scope"):
            md.append(f"- `{k}`: v1 {s1[k]} → v2 {s2[k]}")
        md.append(f"- 카테고리 상태 변화(같은 id): {res['category_transitions']}")
        for f in ("pattern", "detail", "material", "color"):
            d = res[f"{f}_edge_diff"]
            md.append(f"- {f} 엣지(같은 id) 제거 {sum(d['removed'].values())} {dict(list(d['removed'].items())[:12])} · "
                      f"추가 {sum(d['added'].values())} {dict(list(d['added'].items())[:12])}")
        md.append(f"- 한국어 기본색 별칭으로 생긴 색 엣지: {res['color_alias_effect']['edges_from_new_aliases']} "
                  f"{res['color_alias_effect']['by_raw']}")
        md.append("")
    write_json_atomic(out / "audit_v1_v2.json", result)
    write_text_atomic(out / "audit_v1_v2.md", "\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
