"""Vogue 기사 일괄 추출 (T1). 본체: src/trend_agent/ingestion/extraction/batch.py

실행 예:
    python scripts/extract_articles.py --backend codex_cli \
        --in "$VOGUE/worktrees/t1/data/raw/vogue_kr/articles_pool_150.jsonl" \
        --out "$SHARED/extractions/extract-v1/" [--ids 857922,876706] [--limit N] [--concurrency 2]
    python scripts/extract_articles.py --out "$SHARED/extractions/extract-v1/" --report-only
    python scripts/extract_articles.py --out "$SHARED/extractions/extract-v1/" \
        --term-mentions "$SHARED/extractions/term_mentions.jsonl" [--inventory "$SHARED/keywords/inventory.csv"]

- 재개: 출력·`_review/`에 이미 있는 article_id는 건너뛴다(`--retry-review`면 `_review/`만 다시).
- `--emit`이면 시작·완료·실패 때 팀 버스 이벤트(job_started|job_done|job_failed)를 보낸다($TEAM_ROOT/bus/emit.py).
- 종료 코드: 0 = 실패 0건 · 2 = LLM 호출 실패가 남음(다시 실행하면 이어서 한다) · 3 = 연속 실패로 조기 중단.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.extraction.batch import BatchConfig, run_batch, write_report  # noqa: E402

DEFAULT_IN = ROOT / "data" / "raw" / "vogue_kr" / "articles_pool_150.jsonl"


def emit(kind: str, summary: str, to: str) -> None:
    team = os.environ.get("TEAM_ROOT")
    if not team:
        return
    cmd = [sys.executable, str(Path(team) / "bus" / "emit.py"), "--kind", kind, "--to", to, "--summary", summary]
    actor = os.environ.get("TEAM_AGENT")
    if actor:
        cmd += ["--actor", actor]
    try:
        subprocess.run(cmd, check=False, timeout=30, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default=None, help="codex_cli | openai_api (기본 env LLM_BACKEND)")
    ap.add_argument("--model", default=None, help="기본 env LLM_MODEL")
    ap.add_argument("--effort", default=None, help="reasoning effort (codex_cli, 기본 env LLM_REASONING_EFFORT)")
    ap.add_argument("--prompt-version", default="extract-v1")
    ap.add_argument("--in", dest="input", type=Path, default=DEFAULT_IN)
    ap.add_argument("--out", type=Path, required=True, help="$SHARED/extractions/<prompt_version>/")
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
    ap.add_argument("--length-area", action="store_true", help="하의·원피스 length_body 롱→맥시 (결정 L-맥시)")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--term-mentions", type=Path, default=None,
                    help="추출 대신 트렌드 언급 집계: --in 기사 + --out 폴더의 검증 통과 추출 → 이 경로(JSONL)")
    ap.add_argument("--inventory", type=Path, default=None, help="T2 키워드 행렬 CSV (--term-mentions와 함께, 선택)")
    ap.add_argument("--emit", action="store_true", help="job_* 이벤트 보내기")
    ap.add_argument("--emit-to", default="T3,T4,L0")
    args = ap.parse_args(argv)

    if args.out.name != args.prompt_version and not (args.report_only or args.term_mentions):
        print(f"경고: --out 폴더 이름({args.out.name})이 prompt_version({args.prompt_version})과 다르다", file=sys.stderr)
    if args.term_mentions:
        from trend_agent.ingestion.extraction.terms import main as terms_main

        targs = ["--pool", str(args.input), "--extractions", str(args.out), "--out", str(args.term_mentions)]
        if args.inventory:
            targs += ["--inventory", str(args.inventory)]
        return terms_main(targs)
    if args.report_only:
        path = write_report(args.out, prompt_version=args.prompt_version,
                            review_queue=args.review_queue or args.out.parent / "review_queue.jsonl")
        print(path)
        return 0

    cfg = BatchConfig(
        input_path=args.input, out_dir=args.out, backend=args.backend, model=args.model,
        reasoning_effort=args.effort, prompt_version=args.prompt_version, concurrency=args.concurrency,
        ids=[x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None, limit=args.limit,
        retry_review=args.retry_review, use_cache=not args.no_cache, timeout_s=args.timeout,
        review_queue=args.review_queue, max_text_gap=args.max_text_gap, reprocess=args.reprocess,
        length_area=args.length_area,
    )
    label = f"T1 추출 {args.prompt_version} → {args.out}"
    if args.emit:
        emit("job_started", f"{label} 시작 (ids={args.ids or 'all'}, limit={args.limit}, 동시성 {args.concurrency})",
             args.emit_to)
    try:
        summ = run_batch(cfg, progress=lambda s: print(s, flush=True))
    except BaseException as e:  # noqa: BLE001 — 실패 이벤트를 남기고 다시 올린다
        if args.emit:
            emit("job_failed", f"{label} 예외 {type(e).__name__}: {str(e)[:200]}", args.emit_to)
        raise
    msg = (f"{label}: 통과 {summ.passed} · 검토 {summ.review} · 실패 {summ.failed} · 건너뜀 {summ.skipped} · "
           f"{summ.wall_s}s · 보고서 {args.out / 'run_report.md'}")
    print(msg)
    code = 3 if summ.stopped_early else (2 if summ.failed else 0)
    if args.emit:
        emit("job_done" if code == 0 else "job_failed", msg + (" · 연속 실패로 중단" if code == 3 else ""), args.emit_to)
    return code


if __name__ == "__main__":
    sys.exit(main())
