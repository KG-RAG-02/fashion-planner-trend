"""트렌드 언급 집계 → `$SHARED/extractions/term_mentions.jsonl` (T2 트렌드 요약표·T3 Term 계층·Tier4 맥락의 입력).

행 = 용어 1개(키 = `normalize_term()` — 계약 함수, Term id·Style name_norm과 같은 키):
    {"term", "norm", "term_id", "surfaces"[], "sources"[], "generic",
     "article_count", "mention_count", "style_article_count", "title_article_count",
     "months"{YYYY-MM: 기사 수}, "seasons"{2027SS: 기사 수}, "first_seen", "last_seen",
     "evidence"[{article_id, block_idx, role, style_name, style_id, published, seasons, season_source}]}
- 용어 원천(`sources`): `extracted_style`(추출 Style명·aliases, 검증 통과 파일만) · `title_quote`(제목의 ‘…’·'…'·"…"·“…”) ·
  `core_suffix`("-코어", 불용어 제외) · `kfashion_substyle`(K-Fashion 하위 스타일 23, 가이드라인 원문에서 파싱) ·
  `t2_keyword`(T2 `$SHARED/keywords/inventory.csv`가 있으면) · `seed`(E2E 시드 — 0건이어도 행을 남긴다)
- 매칭: 텍스트 블록(문단·소제목)과 제목만(이미지 캡션 제외). 공백 유연 + **앞쪽 단어 경계**(앞 글자가 한글·영문이
  아니어야 한다 — "포스트모던"의 "모던" 제외), 뒤 조사·접미 허용(영문 용어는 뒤도 경계). 영문은 대소문자 무시.
- `role`: 그 기사 추출 Style과 norm이 같으면 `style`(+`style_name`·`style_id` = `<aid>-s<n>`, 적재기 규칙), 제목이면
  `title`(`block_idx: null`), 그 밖은 `mention`("끝났다"는 언급도 mention — Style 수와 구분된다).
  Style 근거 블록은 용어가 글자로 없어도 `style` 근거로 넣는다.
- 시즌: role=style이면 그 Style `seasons`(`season_source: "style"`), 아니면 기사 태그·제목 시즌(`article_<source>`)만.
  추정하지 않는다. 월 = `published_at[:7]`(라벨 `published`와 같은 기준). 인용 문장은 넣지 않는다(블록 번호만).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ...config import ROOT
from ...schemas.graph_schema import normalize_term, term_id

__all__ = ["TermSource", "collect_terms", "build_term_mentions", "parse_kfashion_substyles", "title_quotes",
           "core_words", "SEEDS", "CORE_STOPWORDS", "main"]

KFASHION_DOC = ROOT / "docs" / "schemas" / "sources" / "kfashion_guideline.md"
SEEDS = ("조용한 럭셔리", "드뮤어", "발레코어", "고프코어", "놈코어")
CORE_STOPWORDS = {"스코어", "하드코어", "애플코어", "코어", "에코어"}  # 점수·운동·브랜드성 단어 (트렌드어 아님)
SOURCE_ORDER = ("extracted_style", "title_quote", "core_suffix", "kfashion_substyle", "t2_keyword", "seed")
QUOTE_RE = re.compile(r"‘([^’]{1,30})’|'([^']{1,30})'|\"([^\"]{1,30})\"|“([^”]{1,30})”")
CORE_RE = re.compile(r"([가-힣A-Za-z]{1,10})\s?코어(?!스)")
GENERIC_RE = re.compile(r"^(이것|이걸|이게|이런|그것|그런|저것|이\s|그\s|저\s)")
HANGUL_LATIN = "가-힣A-Za-z"


@dataclass
class TermSource:
    norm: str
    surfaces: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    generic: bool = False

    def add(self, surface: str, source: str) -> None:
        s = re.sub(r"\s+", " ", surface).strip()
        if s and s not in self.surfaces:
            self.surfaces.append(s)
        self.sources.add(source)


def _text_blocks(article: dict) -> list[dict]:
    return [b for b in article.get("blocks") or [] if b.get("type") in ("paragraph", "heading")]


def title_quotes(title: str) -> list[str]:
    out = []
    for m in QUOTE_RE.finditer(title or ""):
        q = next(g for g in m.groups() if g is not None).strip()
        if q and q not in out:
            out.append(q)
    return out


def core_words(text: str) -> list[str]:
    out = []
    for m in CORE_RE.finditer(text or ""):
        w = m.group(1) + "코어"
        if w not in CORE_STOPWORDS and w not in out:
            out.append(w)
    return out


def parse_kfashion_substyles(path: Path = KFASHION_DOC, expected: int = 23) -> list[str]:
    """§13 "10가지 베이직 스타일, 23가지 하위 스타일" 줄에서 괄호 안 하위 스타일을 읽는다. 개수가 다르면 오류."""
    text = path.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if "23가지 하위 스타일" in l and "트레디셔널(" in l)
    body = line.split("):", 1)[1] if "):" in line else line
    subs: list[str] = []
    for grp in re.findall(r"[가-힣]+\(([^)]*)\)", body):
        for s in grp.split(","):
            s = s.strip()
            if s and s not in subs:
                subs.append(s)
    if len(subs) != expected:
        raise ValueError(f"K-Fashion 하위 스타일 {len(subs)}개 != {expected}: {subs}")
    return subs


def _read_inventory(path: Optional[Path]) -> list[str]:
    if not path or not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r["keyword"].strip() for r in csv.DictReader(f) if (r.get("keyword") or "").strip()]


def _style_norms(style: dict) -> set[str]:
    return {normalize_term(x) for x in [style.get("name") or "", *(style.get("aliases") or [])] if normalize_term(x)}


def collect_terms(articles: list[dict], extractions: dict[str, dict], *, inventory: Optional[Path] = None,
                  kfashion_doc: Path = KFASHION_DOC, seeds: Iterable[str] = SEEDS) -> dict[str, TermSource]:
    terms: dict[str, TermSource] = {}

    def add(surface: str, source: str, generic: bool = False) -> None:
        n = normalize_term(surface)
        if len(n) < 2:
            return
        t = terms.setdefault(n, TermSource(norm=n))
        t.add(surface, source)
        t.generic = t.generic or generic

    for ext in extractions.values():
        for s in ext.get("styles") or []:
            add(s.get("name") or "", "extracted_style")
            for a in s.get("aliases") or []:
                add(a, "extracted_style")
    for a in articles:
        for q in title_quotes(a.get("title") or ""):
            add(q, "title_quote", generic=bool(GENERIC_RE.match(q)))
        for text in [a.get("title") or ""] + [b.get("text") or "" for b in _text_blocks(a)]:
            for w in core_words(text):
                add(w, "core_suffix")
    for sub in parse_kfashion_substyles(kfashion_doc):
        for part in sub.split("/"):
            add(part, "kfashion_substyle")
    for kw in _read_inventory(inventory):
        add(kw, "t2_keyword")
    for s in seeds:
        add(s, "seed")
    return terms


def _surface_regex(surface: str) -> str:
    chars = [c for c in re.sub(r"[\s\-_·]+", "", surface)]
    body = r"[\s\-_·]*".join(re.escape(c) for c in chars)
    tail = r"(?![A-Za-z])" if re.search(r"[A-Za-z]$", surface) else ""
    return rf"(?<![{HANGUL_LATIN}]){body}{tail}"


def _compile(t: TermSource) -> re.Pattern:
    alts = sorted({_surface_regex(s) for s in t.surfaces if len(re.sub(r"\s+", "", s)) >= 2}, key=len, reverse=True)
    return re.compile("|".join(f"(?:{a})" for a in alts), flags=re.IGNORECASE)


def _article_season(a: dict) -> tuple[list[str], Optional[str]]:
    s = a.get("season") or {}
    if s.get("value"):
        return [s["value"]], f"article_{s.get('source') or 'tag'}"
    return [], None


def build_term_mentions(articles: list[dict], extractions: dict[str, dict], *, inventory: Optional[Path] = None,
                        kfashion_doc: Path = KFASHION_DOC, seeds: Iterable[str] = SEEDS) -> list[dict]:
    terms = collect_terms(articles, extractions, inventory=inventory, kfashion_doc=kfashion_doc, seeds=seeds)
    rows = []
    for norm in sorted(terms):
        t = terms[norm]
        pat = _compile(t)
        evidence: list[dict] = []
        mention_count = 0
        art_ids: set[str] = set()
        style_arts: set[str] = set()
        title_arts: set[str] = set()
        months: Counter = Counter()
        seasons: Counter = Counter()
        dates: list[str] = []
        for a in articles:
            aid = str(a["article_id"])
            published = str(a.get("published_at") or "")[:10] or None
            ext = extractions.get(aid) or {}
            style_hit = None
            for n, s in enumerate(ext.get("styles") or []):
                if norm in _style_norms(s):
                    style_hit = (n, s)
                    break
            hits: list[tuple[Optional[int], str]] = []
            title_n = len(pat.findall(a.get("title") or ""))
            if title_n:
                hits.append((None, "title"))
                mention_count += title_n
            for b in _text_blocks(a):
                k = len(pat.findall(b.get("text") or ""))
                if k:
                    hits.append((b["idx"], "mention"))
                    mention_count += k
            if style_hit:
                ev_blocks = {e["block_idx"] for e in style_hit[1].get("evidence") or []}
                have = {bi for bi, _ in hits}
                hits += [(bi, "mention") for bi in sorted(ev_blocks - have)]
            if not hits:
                continue
            art_ids.add(aid)
            if published:
                months[published[:7]] += 1
                dates.append(published)
            if style_hit:
                n, s = style_hit
                style_arts.add(aid)
                s_seasons = [f"{x['year']}{x['season']}" for x in s.get("seasons") or []]
                if s_seasons:
                    a_seasons, src = s_seasons, "style"
                else:
                    a_seasons, src = _article_season(a)
            else:
                a_seasons, src = _article_season(a)
            for se in a_seasons:
                seasons[se] += 1
            for bi, role in sorted(hits, key=lambda x: (-1 if x[0] is None else x[0])):
                if role == "title":
                    title_arts.add(aid)
                elif style_hit:
                    role = "style"
                evidence.append({
                    "article_id": aid, "block_idx": bi, "role": role,
                    "style_name": style_hit[1].get("name") if style_hit and role == "style" else None,
                    "style_id": f"{aid}-s{style_hit[0]}" if style_hit and role == "style" else None,
                    "published": published, "seasons": a_seasons, "season_source": src,
                })
        canonical = t.surfaces[0] if t.surfaces else norm
        rows.append({
            "term": canonical, "norm": norm, "term_id": term_id(norm), "surfaces": t.surfaces,
            "sources": [s for s in SOURCE_ORDER if s in t.sources], "generic": t.generic,
            "article_count": len(art_ids), "mention_count": mention_count,
            "style_article_count": len(style_arts), "title_article_count": len(title_arts),
            "months": dict(sorted(months.items())), "seasons": dict(sorted(seasons.items())),
            "first_seen": min(dates) if dates else None, "last_seen": max(dates) if dates else None,
            "evidence": evidence,
        })
    rows.sort(key=lambda r: (-r["article_count"], r["norm"]))
    return rows


def _load_extractions(folder: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(folder.glob("*.json")):  # _review/ 하위 폴더는 읽지 않는다(검증 통과만)
        doc = json.loads(p.read_text(encoding="utf-8"))
        out[str(doc["label_meta"]["article_id"])] = doc["extraction"]
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="트렌드 언급 집계 → term_mentions.jsonl")
    ap.add_argument("--pool", type=Path, default=ROOT / "data" / "raw" / "vogue_kr" / "articles_pool_150.jsonl")
    ap.add_argument("--extractions", type=Path, required=True, help="$SHARED/extractions/<prompt_version>/")
    ap.add_argument("--inventory", type=Path, default=None, help="T2 $SHARED/keywords/inventory.csv (선택)")
    ap.add_argument("--out", type=Path, required=True, help="$SHARED/extractions/term_mentions.jsonl")
    args = ap.parse_args(argv)
    from .batch import load_articles

    articles = load_articles(args.pool)
    exts = _load_extractions(args.extractions)
    rows = build_term_mentions(articles, exts, inventory=args.inventory)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(args.out)
    by_src = Counter(s for r in rows for s in r["sources"])
    print(f"용어 {len(rows)}개 · 기사 {len(articles)} · 추출 {len(exts)} · 출처별 {dict(by_src)} · "
          f"언급 0건 {sum(1 for r in rows if r['article_count'] == 0)} → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
