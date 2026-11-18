"""Vogue 기사 일괄 추출. 본체: src/trend_agent/ingestion/extraction/batch.py

실행 예:
    python scripts/extract_articles.py --limit 3                       # 3건만 시험 (기본 입력: 크롤 150건)
    python scripts/extract_articles.py --ids 857922,876706             # 기사 지정
    python scripts/extract_articles.py                                 # 입력 기사 전체 → data/processed/extractions/article-extraction/
    python scripts/extract_articles.py --report-only                   # LLM 호출 없이 보고서만

필요: `.env`의 LLM_API_KEY, LLM_MODEL (OpenAI API. 모델명은 코드에 없다). **실행하면 기사 원문이 OpenAI로 전송된다 —
처음 실행하기 전에 사용자에게 확인한다.**

- 재개: 출력·`_review/`에 이미 있는 article_id는 건너뛴다(`--retry-review`면 `_review/`만 다시).
- 출력: `<out>/<article_id>.json`(검증 통과) · `<out>/_review/<article_id>.json`(재시도 후에도 오류) ·
  `<out>/../review_queue.jsonl` · `<out>/run.log` · `<out>/run_report.md`. 호출 기록과 캐시는 `data/processed/llm/`다.
- 종료 코드: 0 = 실패 0건 · 2 = LLM 호출 실패가 남음(다시 실행하면 이어서 한다) · 3 = 연속 실패로 조기 중단.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.extraction.batch import BatchConfig, run_batch, write_report  # noqa: E402
from trend_agent.ingestion.extraction.prompt import PROMPT_ID  # noqa: E402

DEFAULT_IN = ROOT / "data" / "raw" / "vogue_kr" / "articles_pool_150.jsonl"
DEFAULT_PROMPT_VERSION = PROMPT_ID
EXTRACTIONS = ROOT / "data" / "processed" / "extractions"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=None, help="기본 env LLM_MODEL")
    ap.add_argument("--reasoning-effort", default=None,
                    help="추론 모델만. 허용 값은 모델별 등록표 configs/llm_models.yaml의 efforts. "
                         "주지 않으면 API 기본값 (docs/pipeline.md D-30)")
    ap.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    ap.add_argument("--in", dest="input", type=Path, default=DEFAULT_IN)
    ap.add_argument("--out", type=Path, default=None, help="기본 data/processed/extractions/<prompt_version>/")
    ap.add_argument("--ids", default=None, help="쉼표로 구분한 article_id (그 순서대로)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--retry-review", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--timeout", type=float, default=None, help="호출당 초 (기본 env LLM_TIMEOUT_S 또는 600)")
    ap.add_argument("--review-queue", type=Path, default=None, help="기본 <out>/../review_queue.jsonl")
    ap.add_argument("--max-text-gap", type=int, default=1)
    ap.add_argument("--reprocess", action="store_true",
                    help="이미 있는 출력도 다시 만든다(캐시된 LLM 응답으로 후처리·검증만, 캐시에 없을 때만 호출)")
    ap.add_argument("--length-area", action="store_true", help="폐기된 L-맥시(하의·원피스 롱→맥시) 재현용 — 기본 꺼짐, 롱은 롱 (D-38)")
    ap.add_argument("--report-only", action="store_true")
    # 후처리 P1·P2는 채택됐다(2026-11-09, docs/pipeline.md D-27·§6.7) → 기본 켬
    ap.add_argument("--no-dedupe", dest="dedupe", action="store_false", help="P1 중복 합치기를 끈다 (기본 켬)")
    ap.add_argument("--no-name-fix", dest="name_fix", action="store_false", help="P2 이름 끝 '트렌드' 제거를 끈다 (기본 켬)")
    args = ap.parse_args(argv)

    out = args.out or EXTRACTIONS / args.prompt_version
    if out.name != args.prompt_version and not args.report_only:
        print(f"경고: --out 폴더 이름({out.name})이 prompt_version({args.prompt_version})과 다르다", file=sys.stderr)
    if args.report_only:
        path = write_report(out, prompt_version=args.prompt_version,
                            review_queue=args.review_queue or out.parent / "review_queue.jsonl")
        print(path)
        return 0

    cfg = BatchConfig(
        input_path=args.input, out_dir=out, model=args.model, prompt_version=args.prompt_version,
        concurrency=args.concurrency,
        ids=[x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None, limit=args.limit,
        retry_review=args.retry_review, use_cache=not args.no_cache, timeout_s=args.timeout,
        review_queue=args.review_queue, max_text_gap=args.max_text_gap, reprocess=args.reprocess,
        length_area=args.length_area, dedupe=args.dedupe, name_fix=args.name_fix,
        reasoning_effort=args.reasoning_effort,
    )
    summ = run_batch(cfg, progress=lambda s: print(s, flush=True))
    print(f"추출 {args.prompt_version} → {out}: 통과 {summ.passed} · 검토 {summ.review} · 실패 {summ.failed} · "
          f"건너뜀 {summ.skipped} · {summ.wall_s}s · 보고서 {out / 'run_report.md'}")
    return 3 if summ.stopped_early else (2 if summ.failed else 0)


if __name__ == "__main__":
    sys.exit(main())
