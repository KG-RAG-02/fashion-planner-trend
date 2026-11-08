"""키워드 행렬 — 파일들에 실제로 있는 스타일·트렌드 키워드 × 출처 존재·빈도 (T2 작업 4, PLAN R4, 사용자 요구).

실행:  python scripts/build_keyword_matrix.py --out "$SHARED/keywords"
출력:  <out>/inventory.csv              키워드 한 줄 = 출처별 존재·빈도
       <out>/inventory_summary.json     출처별 건수·검증(PLAN §3 수치 재현)·0건 키워드
       <out>/inventory_summary.md       사람용 요약

키워드 출처(origins)
- label_style      dev 라벨(--label-ids, 기본 T4 split dev 8건)의 styles[].name·aliases. **test 라벨은 읽지 않는다**(누출 방지)
- manifest         manifest.yaml styles 문자열의 본명(괄호·콜론 앞) / manifest_item = 괄호 안·콜론 뒤 목록
- vogue_title_quote 기사 제목의 인용어(‘…’ '…' "…" “…”). 지시어(이·그·이것…)는 generic=true
- vogue_core       제목·본문·캡션의 "-코어" 단어(`([가-힣A-Za-z]{1,10})\\s?코어(?!스)` — 마이클 코어스 제외)
- vogue_tag        기사 태그
- kfashion_basic / kfashion_sub   kfashion_guideline.md §13의 베이직 10 · 하위 23
- e2e_seed         E2E 시드(조용한 럭셔리·드뮤어·발레코어·고프코어·놈코어)

빈도 열
- vogue_articles: 제목 또는 본문(문단·소제목 블록)에 나온 기사 수 — 공백 무시 부분 문자열(T1 term_probe와 같은 기준)
- vogue_title_articles / vogue_body_articles / vogue_caption_articles / vogue_tag_articles: 위치별 기사 수
- first_month / months: vogue_articles 기사의 published_at[:7](UTC)
- otp_name_rows / otp_feature_rows: 02 원천 상품명·feature에서 맞은 행 수 — 토큰 규칙(text_match: 공백 넘는 매칭 금지)
- label_style: 그 이름(또는 별칭)을 가진 dev 라벨 Style 수 · manifest_A/A?/L/N: styles 문자열에 그 키워드가 있는 기사 수
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.normalization.alias_map import norm_key  # noqa: E402
from trend_agent.ingestion.normalization.otpensource import _clean, iter_rows  # noqa: E402
from trend_agent.ingestion.normalization.output import resolve_out, write_json_atomic, write_text_atomic  # noqa: E402
from trend_agent.ingestion.normalization.text_match import TextMatcher  # noqa: E402
from trend_agent.schemas.graph_schema import normalize_term  # noqa: E402

DEV_LABEL_IDS = ["857922", "876706", "881396", "882345", "884834", "788698", "879245", "873509"]  # T4 split_meta dev (feature/t4-eval@abb2193)
E2E_SEEDS = ["조용한 럭셔리", "드뮤어", "발레코어", "고프코어", "놈코어"]
QUOTE_RE = re.compile(r"[‘'\"“]([^‘’'\"“”]{1,30})[’'\"”]")
CORE_RE = re.compile(r"([가-힣A-Za-z]{1,10})\s?코어(?!스)")
GENERIC_RE = re.compile(r"^(이|그|저)\s|^(이것|이걸|이거|그것|저것)")
CONTAINER_RE = re.compile(r"트렌드\s*\d|^\d{4}\s*(SS|FW)?\s*:?$|\d+\s*$")
COLUMNS = ["keyword", "norm", "origins", "generic", "label_style", "manifest_A", "manifest_A?", "manifest_L", "manifest_N",
           "vogue_articles", "vogue_title_articles", "vogue_body_articles", "vogue_caption_articles", "vogue_tag_articles",
           "kfashion", "otp_name_rows", "otp_feature_rows", "first_month", "months"]


def squash(text: str) -> str:
    """공백 무시 비교 키: NFKC → 소문자 → 공백 제거."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").lower())


def parse_manifest_style(s: str) -> tuple[str, list[str]]:
    """'뉴 로(New Raw)' → ('뉴 로', ['New Raw']) · '2026FW: 1980년대, 가지색' → ('2026FW', ['1980년대', '가지색'])."""
    s = s.strip()
    if ":" in s:
        head, rest = s.split(":", 1)
        return head.strip(), [x.strip(" …") for x in re.split(r"[,·]", rest) if x.strip(" …")]
    m = re.match(r"^(.*?)\((.*)\)\s*$", s)
    if m:
        head, inner = m.group(1).strip(), m.group(2)
        items = [x.strip(" …") for x in re.split(r"[,·]", inner) if x.strip(" …") and x.strip(" …") != "소제목"]
        return head, items
    return s, []


def kfashion_styles(path: Path) -> tuple[list[str], list[str]]:
    line = next(l for l in path.read_text(encoding="utf-8").splitlines() if "10가지 베이직 스타일" in l)
    body = line.split(":", 1)[1]
    basics, subs = [], []
    for m in re.finditer(r"([가-힣]+)\(([^)]+)\)", body):
        basics.append(m.group(1))
        subs.extend(x.strip() for x in m.group(2).split(","))
    assert len(basics) == 10 and len(subs) == 23, (len(basics), len(subs))
    return basics, subs


class Inventory:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def add(self, keyword: str, origin: str, generic: bool = False) -> None:
        keyword = re.sub(r"\s+", " ", keyword).strip()
        key = squash(keyword)
        if len(key) < 2:
            return
        row = self.rows.setdefault(key, {"keyword": keyword, "origins": set(), "generic": False})
        row["origins"].add(origin)
        row["generic"] = row["generic"] or generic


def build(a: argparse.Namespace) -> dict[str, Any]:
    inv = Inventory()
    # 라벨 Style명 (dev만)
    label_names: Counter = Counter()
    used_labels = []
    for lid in a.label_ids:
        p = Path(a.labels_dir) / f"{lid}.json"
        if not p.exists():
            continue
        used_labels.append(lid)
        for st in json.loads(p.read_text(encoding="utf-8")).get("extraction", {}).get("styles", []):
            for name in [st.get("name")] + list(st.get("aliases") or []):
                if name:
                    inv.add(name, "label_style")
                    label_names[squash(name)] += 1
    # manifest
    manifest = yaml.safe_load(Path(a.manifest).read_text(encoding="utf-8"))["articles"]
    manifest_text: list[tuple[str, str]] = []  # (screen, squashed styles text)
    for art in manifest:
        styles = art.get("styles") or []
        manifest_text.append((str(art.get("screen")), squash(" | ".join(styles))))
        for s in styles:
            head, items = parse_manifest_style(s)
            inv.add(head, "manifest", generic=bool(items and CONTAINER_RE.search(head)))
            for it in items:
                inv.add(it, "manifest_item")
    # 기사
    articles = [json.loads(l) for l in Path(a.articles).read_text(encoding="utf-8").splitlines() if l.strip()]
    core_counter: Counter = Counter()
    quotes: Counter = Counter()
    for art in articles:
        for q in QUOTE_RE.findall(art.get("title") or ""):
            q = q.strip()
            quotes[q] += 1
            inv.add(q, "vogue_title_quote", generic=bool(GENERIC_RE.match(q)))
        texts = [art.get("title") or ""] + [b.get("text") or "" for b in art.get("blocks", []) if b.get("type") in ("paragraph", "heading")] \
            + [im.get("caption") or "" for im in art.get("images", [])]
        found = {m.group(1) + "코어" for t in texts for m in CORE_RE.finditer(t)}
        for w in found:
            inv.add(w, "vogue_core")
            core_counter[w] += 1
        for t in art.get("tags") or []:
            inv.add(t, "vogue_tag")
    basics, subs = kfashion_styles(Path(a.kfashion))
    for s in basics:
        inv.add(s, "kfashion_basic")
    for s in subs:
        inv.add(s, "kfashion_sub")
    for s in E2E_SEEDS:
        inv.add(s, "e2e_seed")

    # 빈도
    art_idx = []
    for art in articles:
        title = squash(art.get("title") or "")
        body = squash(" ".join(b.get("text") or "" for b in art.get("blocks", []) if b.get("type") in ("paragraph", "heading")))
        cap = squash(" ".join(im.get("caption") or "" for im in art.get("images", [])))
        tags = {squash(t) for t in art.get("tags") or []}
        art_idx.append((art, title, body, cap, tags, (art.get("published_at") or "")[:7]))
    otp_rows = list(iter_rows(a.otp_snapshot))
    otp_names = [_clean(r.get("product_name")) for r in otp_rows]
    otp_feats = [_clean(r.get("feature")) for r in otp_rows]

    out_rows = []
    for key, row in sorted(inv.rows.items()):
        kw = row["keyword"]
        months = sorted({m for (art, t, b, c, tg, m) in art_idx if key in t or key in b})
        rec = {
            "keyword": kw,
            "norm": normalize_term(kw),
            "origins": "|".join(sorted(row["origins"])),
            "generic": row["generic"],
            "label_style": label_names.get(key, 0),
            **{f"manifest_{s}": sum(1 for sc, txt in manifest_text if sc == s and key in txt) for s in ("A", "A?", "L", "N")},
            "vogue_articles": sum(1 for (_, t, b, _c, _g, _m) in art_idx if key in t or key in b),
            "vogue_title_articles": sum(1 for x in art_idx if key in x[1]),
            "vogue_body_articles": sum(1 for x in art_idx if key in x[2]),
            "vogue_caption_articles": sum(1 for x in art_idx if key in x[3]),
            "vogue_tag_articles": sum(1 for x in art_idx if key in x[4]),
            "kfashion": "basic" if "kfashion_basic" in row["origins"] else ("sub" if "kfashion_sub" in row["origins"] else ""),
        }
        m = TextMatcher([(norm_key(kw), kw, False)])
        rec["otp_name_rows"] = sum(1 for n in otp_names if m.find(n))
        rec["otp_feature_rows"] = sum(1 for f in otp_feats if m.find(f))
        rec["first_month"] = months[0] if months else ""
        rec["months"] = " ".join(months)
        out_rows.append(rec)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(out_rows)
    out = Path(a.out)
    write_text_atomic(out / "inventory.csv", buf.getvalue())

    by_kw = {squash(r["keyword"]): r for r in out_rows}
    def va(k: str) -> int:
        return by_kw.get(squash(k), {}).get("vogue_articles", 0)
    origin_counts = Counter(o for r in out_rows for o in r["origins"].split("|"))
    checks = {
        "조용한 럭셔리 기사 수(PLAN 13)": va("조용한 럭셔리"),
        "놈코어(T1 F7 3)": va("놈코어"), "발레코어(PLAN 1)": va("발레코어"), "고프코어(PLAN 1)": va("고프코어"),
        "드뮤어(PLAN 0)": va("드뮤어"), "절제된 맥시멀리즘(T1 F8 3)": va("절제된 맥시멀리즘"),
        "제목 인용어 종류(PLAN 43)": len(quotes), "-코어 종류(PLAN 6 · T1 7)": len(core_counter),
        "-코어 기사 수": dict(core_counter.most_common()), "kfashion": [len(basics), len(subs)],
    }
    summary = {
        "inputs": {"articles": a.articles, "manifest": a.manifest, "labels_dir": a.labels_dir, "labels_used": used_labels,
                   "kfashion": a.kfashion, "otp_snapshot": str(a.otp_snapshot)},
        "keywords": len(out_rows), "origins": dict(sorted(origin_counts.items())), "checks": checks,
        "top_vogue": [(r["keyword"], r["vogue_articles"]) for r in sorted(out_rows, key=lambda r: (-r["vogue_articles"], r["keyword"]))[:30]],
        "top_otp_name": [(r["keyword"], r["otp_name_rows"]) for r in sorted(out_rows, key=lambda r: (-r["otp_name_rows"], r["keyword"]))[:20]],
        "zero_vogue": sorted(r["keyword"] for r in out_rows if r["vogue_articles"] == 0 and not r["generic"]),
        "e2e_seeds": {k: {c: by_kw.get(squash(k), {}).get(c) for c in ("vogue_articles", "vogue_title_articles", "first_month",
                                                                        "months", "otp_name_rows", "label_style")} for k in E2E_SEEDS},
    }
    write_json_atomic(out / "inventory_summary.json", summary)
    md = ["# 키워드 행렬 요약", "", f"- 키워드 {summary['keywords']}개 · 출처별 {summary['origins']}",
          f"- 라벨: dev {len(used_labels)}건만({', '.join(used_labels)}) — test 라벨은 읽지 않음", "", "## 검증(PLAN §3·T1 F7/F8)", ""]
    md += [f"- {k}: {v}" for k, v in checks.items()]
    md += ["", "## E2E 시드", ""] + [f"- {k}: {v}" for k, v in summary["e2e_seeds"].items()]
    md += ["", "## Vogue 기사 수 상위 30", "", ", ".join(f"{k} {n}" for k, n in summary["top_vogue"]),
           "", "## 02 상품명 행 수 상위 20", "", ", ".join(f"{k} {n}" for k, n in summary["top_otp_name"]),
           "", f"## Vogue 0건 키워드 ({len(summary['zero_vogue'])})", "", ", ".join(summary["zero_vogue"])]
    write_text_atomic(out / "inventory_summary.md", "\n".join(md) + "\n")
    return summary


def main() -> None:
    import os

    shared = Path(os.environ.get("SHARED", ROOT / "_shared"))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--articles", default=str(shared / "data/raw/vogue_kr/articles_pool_150.jsonl"))
    ap.add_argument("--manifest", default=str(ROOT / "eval/datasets/article_extraction/manifest.yaml"))
    ap.add_argument("--labels-dir", default=str(shared / "labels"))
    ap.add_argument("--label-ids", type=lambda s: s.split(","), default=DEV_LABEL_IDS, help="쉼표 구분(기본: dev 8건)")
    ap.add_argument("--kfashion", default=str(ROOT / "docs/schemas/sources/kfashion_guideline.md"))
    ap.add_argument("--otp-snapshot", default=str(shared / "hf_cache/hub/datasets--hateslopacademy--otpensource_dataset/"
                                                  "snapshots/0e2cb23abedd934fdb10735c8450063a785ff93c"))
    a = ap.parse_args()
    a.out = str(resolve_out(a.out))
    s = build(a)
    print(json.dumps({k: s[k] for k in ("keywords", "origins", "checks", "e2e_seeds")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
