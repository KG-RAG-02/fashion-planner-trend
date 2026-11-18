"""Vogue Korea 패션 트렌드 기사 크롤링 → data/raw/vogue_kr/*.jsonl (교육·연구용 예시)

주의: robots.txt를 확인하고 금지된 주소면 멈추지만, 사이트 이용약관은 작성자들이 검토하지 않았다.
수집한 기사는 재배포하지 않는다. 실행 책임은 실행한 사람에게 있다.

사용:
  python scripts/crawl_vogue_sample.py --limit 5
  # 빠진 기사만 추가 (D-45): 2026-01-01 이후, 기존 풀에 없는 기사만 받아 새 파일에 쓰고, 기존 풀 + 새 기사를 합친 풀을 만든다
  python scripts/crawl_vogue_sample.py --since 2026-01-01 --skip-existing data/raw/vogue_kr/articles_pool_150.jsonl \
      --out data/raw/vogue_kr/articles_new_20261111.jsonl --merged-out data/raw/vogue_kr/articles_pool_2026.jsonl
기존 풀 파일은 고치지 않는다. 합친 풀은 기존 줄을 그대로 복사하고 오류 없는 새 기사만 덧붙인다.
범위는 기사 발행일(published_at) 기준이다: 목록 날짜로는 들어왔어도 발행일이 --since보다 이르면 합친 풀에서 뺀다(--out에는 받은 그대로 남는다).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fashion_planner_trend.ingestion.crawler.vogue_kr import (  # noqa: E402
    DEFAULT_LIMIT,
    RobotsDisallowed,
    crawl,
    existing_ids,
    merge_pools,
    published_since,
    save_jsonl,
)


def main() -> None:
    ap = argparse.ArgumentParser(epilog="robots.txt가 막는 주소면 멈춘다. 수집한 기사는 재배포하지 않는다.")
    ap.add_argument("--limit", type=int, default=None, help=f"최대 기사 수 (--since 없이 쓰면 기본 {DEFAULT_LIMIT})")
    ap.add_argument("--since", help="YYYY-MM-DD 이상인 기사만 (목록 post_date 기준, D-45)")
    ap.add_argument("--skip-existing", action="append", default=[], help="이미 받은 풀 파일 (여러 번 줄 수 있음)")
    ap.add_argument("--delay", type=float, default=1.0, help="요청 간 대기(초)")
    ap.add_argument("--out", default=str(ROOT / "data" / "raw" / "vogue_kr" / "articles_sample.jsonl"))
    ap.add_argument("--merged-out", help="기존 풀(--skip-existing) + 새 기사를 합친 풀 파일")
    args = ap.parse_args()

    limit = args.limit if args.limit is not None else (None if args.since else DEFAULT_LIMIT)
    out = Path(args.out)
    for p in [*args.skip_existing, args.merged_out]:
        if p and Path(p).resolve() == out.resolve():
            sys.exit(f"--out이 기존 풀 또는 합친 풀과 같은 파일입니다: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    skip = existing_ids(args.skip_existing)
    try:
        records = crawl(limit, args.delay, since=args.since, skip_ids=skip)
    except RobotsDisallowed as e:
        sys.exit(f"중단: {e}")
    save_jsonl(records, str(out))
    ok = sum(1 for r in records if not r.get("error"))
    print(f"skip {len(skip)} existing · saved {len(records)} new records ({ok} ok, {len(records) - ok} error) -> {out}")
    if args.merged_out:
        records, before = published_since(records, args.since)  # 범위는 발행일 기준(D-45)
        if before:
            print(f"excluded {len(before)} published before {args.since}: "
                  + ", ".join(f"{r.get('article_id')}({(r.get('published_at') or '')[:10]})" for r in before))
        stats = merge_pools(args.skip_existing, records, args.merged_out)
        print(f"merged: kept {stats['kept']} existing + added {stats['added']} new -> {args.merged_out}")


if __name__ == "__main__":
    main()
