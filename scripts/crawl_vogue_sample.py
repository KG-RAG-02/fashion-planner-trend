"""Vogue Korea 패션 트렌드 기사 N개 크롤링 → data/raw/vogue_kr/*.jsonl

사용: python scripts/crawl_vogue_sample.py --limit 20
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.crawler.vogue_kr import crawl, save_jsonl  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--delay", type=float, default=1.0, help="요청 간 대기(초)")
    ap.add_argument("--out", default=str(ROOT / "data" / "raw" / "vogue_kr" / "articles_sample.jsonl"))
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    records = crawl(args.limit, args.delay)
    save_jsonl(records, args.out)
    ok = sum(1 for r in records if not r.get("error"))
    print(f"saved {len(records)} records ({ok} ok, {len(records) - ok} error) -> {args.out}")


if __name__ == "__main__":
    main()
