"""트렌드 용어(또는 기사) → Trend Resolver(Tier1~4) → 결과 JSON(TrendAnalysisResult v1.1) → (T2) HTML·Obsidian.

실행 예:
  python scripts/run_trend_pipeline.py --trend 시럽코어 퍼플 드뮤어 --top-k 5
  python scripts/run_trend_pipeline.py --article-id 881396
  python scripts/run_trend_pipeline.py --from-extractions "$SHARED/extractions/extract-v2"      # 그 폴더 기사들의 Style
  python scripts/run_trend_pipeline.py --trend 드뮤어 --force-tier 4 --tier4-context none       # 평가 실행(T4)
  python scripts/run_trend_pipeline.py --trend 레이디라이크 --hide-style 855421-s0 --no-llm      # 숨김 평가(T4)
출력: 요약은 화면, 전체 JSON은 <--out>/<request_id>.json (기본 $SHARED/results, 없으면 data/processed/results).
선행: Neo4j(그래프 적재: scripts/load_graph.py). Tier4는 T1 llm 구현(LLM_BACKEND·LLM_MODEL), Tier3는 C1 임베딩과 τ·δ.
      develop 병합 전에는 --llm-impl / --emb-impl로 구현 폴더(예: worktrees/t1/src/trend_agent/llm)를 읽기 전용으로 붙인다.
읽기만 한다(Neo4j 쓰기 없음). Tier4 캐시는 term_map.jsonl에 profile 행을 더한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neo4j import GraphDatabase  # noqa: E402

from trend_agent.config import neo4j_settings  # noqa: E402
from trend_agent.graph.loader import read_crawl  # noqa: E402
from trend_agent.graph.search import TrendSearcher  # noqa: E402
from trend_agent.resolver import ResolveConfig, TrendResolver  # noqa: E402
from trend_agent.resolver import term_map as tm  # noqa: E402
from trend_agent.resolver import tier4 as t4  # noqa: E402
from trend_agent.retrieval.weights import ResolverConfig, Tier3Config, load_weights  # noqa: E402
from trend_agent.schemas import vocabulary as V  # noqa: E402
from trend_agent.schemas.trend_analysis_result import TrendAnalysisResult  # noqa: E402
from trend_agent.schemas.trend_query import CategoryFilter, CategoryRef, SeasonRequest  # noqa: E402

CRAWL = ROOT / "data" / "raw" / "vogue_kr" / "articles_pool_150.jsonl"


def category_ref(name: str) -> CategoryRef:
    if name in {m.value for m in V.CategoryMinor}:
        return CategoryRef(raw=name, minor=V.CategoryMinor(name))
    if name in {m.value for m in V.CategoryMajor}:
        return CategoryRef(raw=name, major=V.CategoryMajor(name))
    return CategoryRef(raw=name)


def parse_season(text: str | None) -> SeasonRequest | None:
    if not text:
        return None
    return SeasonRequest(year=int(text[:4]), season=V.Season(text[4:].upper()), raw=text, origin="user_requested")


def attach_impl(package: str, impl_dir: str | None) -> None:
    """지연 로딩 계약 패키지(trend_agent.llm / trend_agent.embeddings)의 검색 경로에 구현 폴더를 덧붙인다(병합 전 스모크용)."""
    if not impl_dir:
        return
    import importlib

    mod = importlib.import_module(package)
    mod.__path__.append(str(Path(impl_dir).resolve()))  # type: ignore[attr-defined]


def default_out() -> Path:
    return Path(os.environ["SHARED"]) / "results" if os.environ.get("SHARED") else ROOT / "data" / "processed" / "results"


def summarize(res: TrendAnalysisResult) -> str:
    out = [f"status={res.status}  schema={res.schema_version}  season_policy={res.applied_settings.season_policy if res.applied_settings else '-'}"]
    for r in res.resolutions:
        out.append(f"\n■ {r.term} [{r.status}] tier={r.tier} mode={r.match_mode} qual={r.qualification}"
                   + (f" sim={r.similarity}" if r.similarity is not None else ""))
        out.append("  steps: " + " → ".join(f"T{s.tier}:{s.outcome}:{s.code}" for s in r.steps))
        if r.llm:
            out.append(f"  llm: {r.llm.backend}/{r.llm.model} {r.llm.prompt_version} cache={r.llm.cache} evidence={r.llm.evidence}")
        for p in r.top_products:
            img = "img" if p.has_image else "---"
            out.append(f"   {p.rank:>2}. {p.trend_score:.3f} [{img}] {p.source}:{(p.name or p.product_id)[:40]}  ← {p.item_name}"
                       + (f" (+{len(p.also_from_items)})" if p.also_from_items else ""))
    by_id = {t.style.id: t for t in res.trends}
    for t in by_id.values():
        s = t.style
        out.append(f"\n  · {s.name} ({s.id}) season={s.season_status} items={len(t.items)} "
                   f"focus core={s.focus_core} supporting={s.focus_supporting}")
    if res.limitations:
        out.append("\n한계·알림:")
        out += [f"  - {x}" for x in res.limitations]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--trend", nargs="+", help="트렌드·스타일 용어(여러 개)")
    src.add_argument("--article-id", nargs="+", help="그 기사의 Style 전부")
    src.add_argument("--from-extractions", metavar="DIR", help="자동 추출 폴더의 기사들(*.json) — 먼저 load_graph.py로 적재")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out", help="결과 폴더 (기본 $SHARED/results)")
    ap.add_argument("--request-id")
    ap.add_argument("--season", help="예: 2027SS (없으면 다음 패션 시즌)")
    ap.add_argument("--season-policy", choices=["strict", "report"], help="기본 weights.yaml flags.season_policy")
    ap.add_argument("--include", nargs="*", help="카테고리(상분류·하분류) 포함")
    ap.add_argument("--exclude", nargs="*", help="카테고리 제외")
    ap.add_argument("--force-tier", type=int, choices=[1, 2, 3, 4], help="평가 실행: 그 Tier만 시도")
    ap.add_argument("--hide-style", nargs="*", default=[], help="평가 실행: Style id(또는 이름)의 name_norm 그룹 숨김")
    ap.add_argument("--tier4-context", choices=["none", "mentions"], default="mentions")
    ap.add_argument("--exclude-context-article", nargs="*", default=[],
                    help="평가 실행: Tier4 맥락에서 뺄 기사 id (--hide-style의 출처 기사는 자동으로 뺀다)")
    ap.add_argument("--no-llm", action="store_true", help="Tier4 LLM 호출 안 함(캐시는 읽음)")
    ap.add_argument("--terms", help="term_map.jsonl (기본 $SHARED/terms/term_map.jsonl)")
    ap.add_argument("--mentions", help="term_mentions.jsonl (기본 $SHARED/extractions/term_mentions.jsonl)")
    ap.add_argument("--crawl", default=str(CRAWL))
    ap.add_argument("--tier3-tau", type=float, help="평가 비교용: Tier3 τ를 이 값으로(weights.yaml은 바꾸지 않음, 결과 applied_settings.tier3에 기록)")
    ap.add_argument("--tier3-delta", type=float, help="평가 비교용: Tier3 δ")
    ap.add_argument("--tier3-model", default=None, help="applied_settings.tier3.model에 남길 임베딩 모델명")
    ap.add_argument("--llm-impl", help="trend_agent.llm 구현 폴더 (develop 병합 전 스모크용, 읽기 전용)")
    ap.add_argument("--emb-impl", help="trend_agent.embeddings 구현 폴더 (develop 병합 전 스모크용, 읽기 전용)")
    a = ap.parse_args()

    attach_impl("trend_agent.llm", a.llm_impl)
    attach_impl("trend_agent.embeddings", a.emb_impl)
    request_id = a.request_id or f"req-{uuid.uuid4().hex[:8]}"
    cfg = ResolveConfig(season=parse_season(a.season), season_policy=a.season_policy,
                        categories=CategoryFilter(include=[category_ref(x) for x in a.include or []],
                                                  exclude=[category_ref(x) for x in a.exclude or []]),
                        top_k=a.top_k, force_tier=a.force_tier, hide_styles=a.hide_style,
                        tier4_context=a.tier4_context, use_llm=not a.no_llm,
                        exclude_context_articles=a.exclude_context_article)
    terms_path = Path(a.terms) if a.terms else tm.default_path()
    mentions_path = Path(a.mentions) if a.mentions else t4.default_mentions_path()
    weights = load_weights()
    override = a.tier3_tau is not None or a.tier3_delta is not None
    if override:
        if a.tier3_tau is None or a.tier3_delta is None:
            ap.error("--tier3-tau와 --tier3-delta는 함께 준다")
        weights = weights.model_copy(update={"resolver": ResolverConfig(tier3=Tier3Config(
            tau=a.tier3_tau, delta=a.tier3_delta, model=a.tier3_model or weights.resolver.tier3.model))})
    s = neo4j_settings()
    try:
        with GraphDatabase.driver(s.uri, auth=(s.user, s.password), notifications_min_severity="OFF") as driver:
            driver.verify_connectivity()
            resolver = TrendResolver(TrendSearcher(driver, weights=weights, top_k=a.top_k), cfg, mentions=t4.read_mentions(mentions_path),
                                     crawl=read_crawl(a.crawl), term_map_path=terms_path)
            if a.trend:
                res = resolver.run(a.trend, request_id)
            else:
                ids = a.article_id or sorted(p.stem for p in Path(a.from_extractions).glob("*.json"))
                res = resolver.run_articles(ids, request_id)
        if override:
            res.limitations.append(f"평가 비교 실행: Tier3 τ={a.tier3_tau}·δ={a.tier3_delta}를 CLI로 지정했습니다"
                                   "(configs/weights.yaml 값이 아님 — 운영 결과와 구분).")
    except Exception as e:  # noqa: BLE001 — 시스템 오류는 status=error로 남긴다 (계약 §1.1)
        res = TrendAnalysisResult(request_id=request_id, status="error",
                                  limitations=[f"시스템 오류로 판정하지 못했습니다: {type(e).__name__}: {str(e)[:300]}"])
    out_dir = Path(a.out) if a.out else default_out()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{request_id}.json"
    path.write_text(res.model_dump_json(indent=2), encoding="utf-8")
    print(summarize(res))
    print(f"\n전체 결과: {path}")
    return 0 if res.status != "error" else 2


if __name__ == "__main__":
    sys.exit(main())
