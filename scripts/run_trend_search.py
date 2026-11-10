"""그래프에서 트렌드와 비슷한 상품을 찾아 `TrendAnalysisResult`로 출력한다 (검색 조건은 인자로 준다).

실행 예:
  python scripts/run_trend_search.py                                  # 시즌 미지정 → 다음 패션 시즌, 필터 없음
  python scripts/run_trend_search.py --season 2027SS --include 스커트 --top-k 3
  python scripts/run_trend_search.py --include 상의 --exclude 후드티 --color 퍼플
  python scripts/run_trend_search.py --style 시럽코어 --json out.json
선행: docker compose up -d, python scripts/load_graph.py
출력: 요약을 화면에, 전체 결과 JSON을 data/processed/results/<request_id>.json (또는 --json 경로)에 쓴다.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from trend_agent.config import neo4j_driver, neo4j_settings  # noqa: E402
from trend_agent.graph.search import TrendSearcher  # noqa: E402
from trend_agent.schemas import vocabulary as V  # noqa: E402
from trend_agent.schemas.trend_query import (  # noqa: E402
    CategoryFilter,
    CategoryRef,
    Requested,
    RequestedAttribute,
    SeasonRequest,
    TermRef,
    TrendQuery,
    TrendTerms,
)


def category_ref(name: str) -> CategoryRef:
    if name in {m.value for m in V.CategoryMinor}:
        return CategoryRef(raw=name, minor=V.CategoryMinor(name))
    if name in {m.value for m in V.CategoryMajor}:
        return CategoryRef(raw=name, major=V.CategoryMajor(name))
    return CategoryRef(raw=name)  # 지원 밖


def parse_season(text: str | None) -> SeasonRequest:
    if not text:
        return SeasonRequest.default_next(date.today())
    return SeasonRequest(year=int(text[:4]), season=V.Season(text[4:].upper()), raw=text, origin="user_requested")


def build_query(a: argparse.Namespace) -> TrendQuery:
    attrs = [RequestedAttribute(type=t, raw=v, value=v) for t in ("color", "material", "pattern", "detail", "fit")
             for v in (getattr(a, t) or [])]
    return TrendQuery(
        request_id=f"req-{uuid.uuid4().hex[:8]}", original_query=" ".join(sys.argv[1:]) or "(인자 없음)",
        trend_terms=TrendTerms(season=parse_season(a.season), style_names=[TermRef(raw=s) for s in (a.style or [])]),
        requested=Requested(categories=CategoryFilter(include=[category_ref(x) for x in a.include or []],
                                                      exclude=[category_ref(x) for x in a.exclude or []]), attributes=attrs))


def summarize(res) -> str:
    out = [f"status={res.status}  시즌 {res.applied_filters.season.year}{res.applied_filters.season.season.value}" if res.applied_filters else f"status={res.status}"]
    for t in res.trends:
        s = t.style
        out.append(f"\n■ {s.name}  seasons={s.seasons}  focus core={s.focus_core} supporting={s.focus_supporting}")
        for it in t.items:
            attrs = [f"색 {','.join(c.value for c in it.colors)}" if it.colors else "", f"소재 {','.join(it.materials)}" if it.materials else "",
                     f"패턴 {','.join(it.patterns)}" if it.patterns else "", f"디테일 {','.join(d.name for d in it.details)}" if it.details else "",
                     f"핏 {it.fit.value}" if it.fit else ""]
            minor = it.category_minor.value if it.category_minor else "-"
            out.append(f"  · {it.name} [{it.category_major.value}/{minor}] " + " ".join(x for x in attrs if x))
            for p in it.similar_products:
                m = p.match
                out.append(f"      {p.score:.3f}  {p.source}:{p.name or p.product_id}  일치 {m.matched}  불일치 {m.mismatched}  미확인 {m.unknown}")
    if res.limitations:
        out.append("\n한계·알림:")
        out += [f"  - {x}" for x in res.limitations]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", help="예: 2027SS (없으면 다음 패션 시즌)")
    ap.add_argument("--include", nargs="*", help="카테고리(상분류 또는 하분류) 포함")
    ap.add_argument("--exclude", nargs="*", help="카테고리 제외")
    for t in ("color", "material", "pattern", "detail", "fit"):
        ap.add_argument(f"--{t}", nargs="*", help=f"요청 {t} (표준 값)")
    ap.add_argument("--style", nargs="*", help="트렌드명")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--json", help="결과 JSON 경로")
    a = ap.parse_args()

    q = build_query(a)
    s = neo4j_settings()
    with neo4j_driver(s) as driver:
        res = TrendSearcher(driver, top_k=a.top_k).search(q)
    path = Path(a.json) if a.json else ROOT / "data" / "processed" / "results" / f"{q.request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(res.model_dump_json(indent=2), encoding="utf-8")
    print(summarize(res))
    print(f"\n전체 결과: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
