"""02·03 v2 Product를 하나의 통합본으로 합친다 (D-33). 그래프 적재(load_graph.py)의 기본 입력이다.

실행 순서:
  python scripts/build_otpensource.py      # → data/processed/v2/otpensource_all.jsonl (image_url 없는 행 제외)
  python scripts/build_musinsa_snap.py     # → data/processed/v2/musinsa_snap_all.jsonl (snap_id·image_ref)
  python scripts/build_musinsa_dataset.py  # → data/processed/v2/musinsa_dataset_v2.jsonl
옵션:  [--in-dir DIR] [--out DIR]   # 둘 다 기본 data/processed/v2
출력(--out):
  musinsa_dataset_v2.jsonl          ProductRecord 한 줄에 하나(02 다음 03). 모든 줄의 키가 같다. 출처는 node.source
  musinsa_dataset_v2_report.json    출처별 건수·이미지 참조 수·필드 목록
  BUILD_INFO.json                   "musinsa_dataset_v2" 항목(입력·출력 sha256)
검사(출처 불일치·id 중복·키 집합 불일치)가 실패하면 아무것도 쓰지 않고 멈춘다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.normalization.combine import OUTPUT_NAME, REPORT_NAME, SOURCES, combine_products  # noqa: E402
from trend_agent.ingestion.normalization.output import (  # noqa: E402
    git_state,
    resolve_out,
    sha256_file,
    update_build_info,
    write_json_atomic,
    write_lines_atomic,
)

V2 = ROOT / "data" / "processed" / "v2"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in-dir", default=str(V2), help="출처별 v2 JSONL 폴더 (기본 data/processed/v2)")
    ap.add_argument("--out", default=str(V2), help="출력 폴더 (기본 data/processed/v2)")
    a = ap.parse_args()
    in_dir = Path(a.in_dir)
    inputs = [(src, in_dir / name) for src, name in SOURCES]
    missing = [str(p) for _, p in inputs if not p.exists()]
    if missing:
        sys.exit(f"입력 없음: {missing} (build_otpensource.py·build_musinsa_snap.py를 먼저 실행)")
    out = resolve_out(a.out)
    lines, report = combine_products(inputs)
    write_lines_atomic(out / OUTPUT_NAME, lines)
    write_json_atomic(out / REPORT_NAME, report)
    update_build_info(out, "musinsa_dataset_v2", {
        "inputs": {str(p.name): sha256_file(p) for _, p in inputs},
        "git": git_state(ROOT),
        "outputs": {n: sha256_file(out / n) for n in (OUTPUT_NAME, REPORT_NAME)},
        "rows": report["rows"],
    })
    print(json.dumps({k: v for k, v in report.items() if k != "fields"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
