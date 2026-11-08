"""트렌드 요약표 — 트렌드별 기사 수·월·시즌·판정 방식(match_mode)·대표 근거 (T2 작업 5 · PLAN R2).

실행:  python scripts/render_trend_summary.py --term-mentions "$SHARED/extractions/term_mentions.jsonl" \\
           [--results R1.json …] [--out "$SHARED/results"] [--vault "$VAULT"]
       T1 term_mentions가 아직 없으면 --inventory "$SHARED/keywords/inventory.csv"로 **임시(degraded)** 표를 만든다.
출력:  <out>/trend_summary.html · <vault>/10_Trends/_트렌드 요약.md (generated: true — 사람이 고친 노트면 건너뜀)

입력
- term_mentions.jsonl(T1 설계 §4.7): term·norm·article_count·mention_count·style_article_count·months{}·seasons{}·
  first_seen·last_seen·evidence[{article_id, block_idx, role(style|title|mention), style_name, published, seasons}]
- --inventory(대체): T2 키워드 행렬의 vogue_articles·months(공백 무시 부분 문자열). role·Style 수·시즌은 기사 메타에서만
  (태그·제목 시즌 — 추정하지 않는다) 계산하고, 근거 블록은 기사에서 직접 찾는다.
- --results: 결과 JSON(v1.1)의 resolutions[] → 용어별 Tier·match_mode·qualification(없으면 "—")
- --articles: 크롤 JSONL — 근거 블록 원문·제목·URL(인용은 로컬 산출물에만, Vogue 이미지는 쓰지 않는다)
결정적: 정렬 고정(기사 수↓·용어), 시각 대신 입력 sha256.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.normalization.output import resolve_out, write_text_atomic  # noqa: E402
from trend_agent.schemas.graph_schema import normalize_term  # noqa: E402

SUMMARY_NOTE = "10_Trends/_트렌드 요약.md"
INVENTORY_ORIGINS = {"label_style", "manifest", "vogue_title_quote", "vogue_core", "e2e_seed"}  # K-Fashion 분류어만인 행은 트렌드어가 아니라 뺀다
ROLE_ORDER = {"style": 0, "title": 1, "mention": 2}


def squash(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").lower())


def snippet(text: str, term: str, width: int = 90) -> str:
    """term 주변 짧은 인용(공백 무시로 위치를 찾고 원문 범위를 자른다)."""
    t = text or ""
    key = squash(term)
    idx_map = [i for i, ch in enumerate(t) if not ch.isspace()]
    sq = "".join(t[i] for i in idx_map).lower()
    pos = unicodedata.normalize("NFKC", sq).find(key)
    if pos < 0 or not idx_map:
        return t[:width] + ("…" if len(t) > width else "")
    start = idx_map[min(pos, len(idx_map) - 1)]
    a = max(0, start - width // 3)
    b = min(len(t), a + width)
    return ("…" if a else "") + t[a:b].strip() + ("…" if b < len(t) else "")


def load_articles(path: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            a = json.loads(line)
            out[str(a["article_id"])] = a
    return out


def block_text(art: dict[str, Any], block_idx: Optional[int]) -> str:
    if block_idx is None:
        return art.get("title") or ""
    for b in art.get("blocks", []):
        if b.get("idx") == block_idx:
            return b.get("text") or ""
    return ""


def resolution_modes(results: list[Path]) -> dict[str, dict[str, Any]]:
    """norm → {tier, match_mode, qualification, request_id} (여러 결과면 tier가 낮은 것)."""
    out: dict[str, dict[str, Any]] = {}
    for p in results:
        d = json.loads(p.read_text(encoding="utf-8"))
        for r in d.get("resolutions") or []:
            norm = r.get("norm") or normalize_term(r.get("term", ""))
            cur = {"tier": r.get("tier"), "match_mode": r.get("match_mode"), "qualification": r.get("qualification"),
                   "status": r.get("status"), "request_id": d.get("request_id")}
            prev = out.get(norm)
            if prev is None or (cur["tier"] or 9) < (prev["tier"] or 9):
                out[norm] = cur
        for t in d.get("trends") or []:  # v1.0 결과: 트렌드 이름으로 graph 판정만 알 수 있다
            norm = normalize_term(t["style"]["name"])
            if norm not in out and t.get("match_mode"):
                out[norm] = {"tier": None, "match_mode": t.get("match_mode"), "qualification": t.get("qualification"),
                             "status": "resolved", "request_id": d.get("request_id")}
    return out


def rows_from_term_mentions(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rows.append({"term": r["term"], "norm": r.get("norm") or normalize_term(r["term"]),
                     "article_count": int(r.get("article_count") or 0), "mention_count": r.get("mention_count"),
                     "style_article_count": r.get("style_article_count"), "months": dict(r.get("months") or {}),
                     "seasons": dict(r.get("seasons") or {}), "evidence": list(r.get("evidence") or []),
                     "origins": "|".join(r.get("sources") or [])})
    return rows


def rows_from_inventory(path: Path, articles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            origins = set(r["origins"].split("|"))
            if r["generic"] == "True" or not origins & INVENTORY_ORIGINS:
                continue
            if int(r["vogue_articles"]) == 0 and "e2e_seed" not in origins:
                continue
            key = squash(r["keyword"])
            months: Counter = Counter()
            seasons: Counter = Counter()
            evidence = []
            for aid in sorted(articles):
                a = articles[aid]
                hits = []
                if key in squash(a.get("title") or ""):
                    hits.append((None, "title"))
                hits += [(b.get("idx"), "mention") for b in a.get("blocks", [])
                         if b.get("type") in ("paragraph", "heading") and key in squash(b.get("text") or "")]
                if not hits:
                    continue
                months[(a.get("published_at") or "")[:7]] += 1
                season = (a.get("season") or {}).get("value")
                if season:
                    seasons[season] += 1
                for bidx, role in hits:
                    evidence.append({"article_id": aid, "block_idx": bidx, "role": role,
                                     "published": (a.get("published_at") or "")[:10], "seasons": [season] if season else []})
            rows.append({"term": r["keyword"], "norm": r["norm"], "article_count": sum(months.values()),
                         "mention_count": len(evidence), "style_article_count": None, "months": dict(sorted(months.items())),
                         "seasons": dict(sorted(seasons.items())), "evidence": evidence, "origins": r["origins"]})
    return rows


def pick_evidence(row: dict[str, Any], n: int = 2) -> list[dict[str, Any]]:
    """대표 근거: role(style > title > mention) → 최근 기사 우선, 기사당 1개."""
    ev = sorted(row["evidence"], key=lambda e: (ROLE_ORDER.get(e.get("role"), 9), -(int(re.sub(r"\D", "", e.get("published") or "0") or 0)),
                                                 str(e.get("article_id")), e.get("block_idx") if e.get("block_idx") is not None else -1))
    out, seen = [], set()
    for e in ev:
        if e["article_id"] in seen:
            continue
        seen.add(e["article_id"])
        out.append(e)
        if len(out) >= n:
            break
    return out


def build_rows(rows: list[dict[str, Any]], articles: dict[str, dict[str, Any]], modes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        m = modes.get(r["norm"]) or {}
        evs = []
        for e in pick_evidence(r):
            a = articles.get(str(e["article_id"]), {})
            evs.append({"title": a.get("title") or e["article_id"], "url": a.get("url"), "published": e.get("published"),
                        "role": e.get("role"), "quote": snippet(block_text(a, e.get("block_idx")), r["term"]),
                        "style_name": e.get("style_name")})
        out.append({**r, "tier": m.get("tier"), "match_mode": m.get("match_mode"), "qualification": m.get("qualification"),
                    "resolution_status": m.get("status"), "top_evidence": evs})
    out.sort(key=lambda x: (-x["article_count"], x["norm"]))
    return out


def fmt_counter(d: dict[str, int]) -> str:
    return " · ".join(f"{k} ×{v}" for k, v in sorted(d.items())) or "—"


def render_html(rows: list[dict[str, Any]], mode: str, src_sha: str) -> str:
    e = lambda v: html.escape("" if v is None else str(v), quote=True)  # noqa: E731
    head = ("<tr><th>트렌드</th><th>기사 수</th><th>언급</th><th>Style 기사</th><th>월</th><th>시즌</th><th>판정 방식</th>"
            "<th>대표 근거</th><th>출처</th></tr>")
    body = []
    for r in rows:
        evs = "".join(
            f'<div class="ev">“{e(x["quote"])}” — ' + (f'<a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x["title"])}</a>'
                                                       if x["url"] else e(x["title"]))
            + f' <span class="meta">{e(x["published"])} · {e(x["role"])}</span></div>' for x in r["top_evidence"])
        mm = f'{e(r["match_mode"])}{" (Tier " + e(r["tier"]) + ")" if r["tier"] else ""}' if r["match_mode"] else "—"
        zero = ' class="zero"' if r["article_count"] == 0 else ""
        body.append(f'<tr{zero}><td><b>{e(r["term"])}</b><br><code>{e(r["norm"])}</code></td><td>{r["article_count"]}</td>'
                    f'<td>{e(r["mention_count"] if r["mention_count"] is not None else "—")}</td>'
                    f'<td>{e(r["style_article_count"] if r["style_article_count"] is not None else "—")}</td>'
                    f'<td>{e(fmt_counter(r["months"]))}</td><td>{e(fmt_counter(r["seasons"]))}</td><td>{mm}</td>'
                    f'<td>{evs or ("코퍼스에 없음" if r["article_count"] == 0 else "—")}</td><td class="meta">{e(r["origins"])}</td></tr>')
    note = ("" if mode == "term_mentions" else
            '<p class="warn">임시 표(degraded): T1 term_mentions 이전이라 기사 수는 T2 키워드 행렬(공백 무시 부분 문자열) 기준이고, '
            'Style 기사 수(role=style)는 비어 있으며 시즌은 기사 태그·제목 시즌만이다. term_mentions가 나오면 다시 만든다.</p>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>트렌드 요약표</title>
<style>body{{font-family:-apple-system,"Apple SD Gothic Neo",sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;font-size:12px}} td,th{{border:1px solid #ddd;padding:4px 6px;vertical-align:top}}
th{{background:#f3f4f6;position:sticky;top:0}} .meta{{color:#777;font-size:11px}} .ev{{margin:2px 0}}
.warn{{color:#b45309}} tr.zero td{{background:#fff7ed}} code{{font-size:10px;color:#666}}</style></head><body>
<h1>트렌드 요약표</h1><p class="meta">입력 {e(mode)} · sha256 {src_sha[:12]} · 트렌드 {len(rows)}개 · Vogue 기사 이미지는 쓰지 않는다(링크만)</p>
{note}<table>{head}{''.join(body)}</table></body></html>
"""


def render_md(rows: list[dict[str, Any]], mode: str, src_sha: str) -> str:
    def cell(v: Any) -> str:
        return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")
    lines = ["---", "type: \"trend_summary\"", "generated: true", f"source_hash: \"{src_sha[:16]}\"", f"input: \"{mode}\"",
             "tags:", "  - \"trend\"", "---", "# 트렌드 요약", ""]
    if mode != "term_mentions":
        lines += ["> [!warning] 임시 표(degraded) — T1 `term_mentions.jsonl` 이전. 기사 수는 T2 키워드 행렬 기준, Style 기사 수 없음.", ""]
    lines += ["| 트렌드 | 기사 수 | 언급 | 월 | 시즌 | 판정 방식 | 대표 근거 |", "|---|---:|---:|---|---|---|---|"]
    for r in rows:
        evs = "<br>".join(f"“{cell(x['quote'])}” — [{cell(x['title']).replace('[', '(').replace(']', ')')}]({x['url']})"
                          if x["url"] else f"“{cell(x['quote'])}” — {cell(x['title'])}" for x in r["top_evidence"])
        mm = f"{r['match_mode']}" + (f" (Tier {r['tier']})" if r["tier"] else "") if r["match_mode"] else "—"
        lines.append(f"| {cell(r['term'])} | {r['article_count']} | {cell(r['mention_count'])} | {cell(fmt_counter(r['months']))} | "
                     f"{cell(fmt_counter(r['seasons']))} | {cell(mm)} | {evs or ('코퍼스에 없음' if r['article_count'] == 0 else '—')} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    shared = Path(os.environ.get("SHARED", ROOT.parent.parent / "_shared"))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--term-mentions", help="T1 term_mentions.jsonl")
    src.add_argument("--inventory", help="T2 keywords/inventory.csv (임시)")
    ap.add_argument("--articles", default=str(shared / "data/raw/vogue_kr/articles_pool_150.jsonl"))
    ap.add_argument("--results", nargs="*", default=[], help="결과 JSON(v1.1) — 용어별 Tier·match_mode")
    ap.add_argument("--out", default=str(shared / "results"))
    ap.add_argument("--vault", default=os.environ.get("VAULT"))
    a = ap.parse_args()
    articles = load_articles(Path(a.articles))
    mode = "term_mentions" if a.term_mentions else "inventory"
    src_path = Path(a.term_mentions or a.inventory)
    rows = rows_from_term_mentions(src_path) if a.term_mentions else rows_from_inventory(src_path, articles)
    modes = resolution_modes([Path(p) for p in a.results])
    built = build_rows(rows, articles, modes)
    h = hashlib.sha256(src_path.read_bytes())
    for p in a.results:
        h.update(Path(p).read_bytes())
    sha = h.hexdigest()
    out = resolve_out(a.out)
    write_text_atomic(out / "trend_summary.html", render_html(built, mode, sha))
    note_status = "no_vault"
    if a.vault:
        path = Path(a.vault) / SUMMARY_NOTE
        if path.exists() and not re.search(r"^generated:\s*true\s*$", path.read_text(encoding="utf-8").split("\n---", 1)[0], re.M):
            note_status = "skipped_human"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = render_md(built, mode, sha)
            note_status = "unchanged" if path.exists() and path.read_text(encoding="utf-8") == text else "written"
            if note_status == "written":
                write_text_atomic(path, text)
    print(json.dumps({"mode": mode, "rows": len(built), "html": str(out / "trend_summary.html"), "note": note_status,
                      "with_match_mode": sum(1 for r in built if r["match_mode"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
