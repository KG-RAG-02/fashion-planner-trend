"""03 musinsa-snap-new를 표준 어휘로 정규화해 전체를 저장한다 (02와 같은 AliasMap).

실행:  python scripts/build_musinsa_snap.py
출력:  data/processed/musinsa_snap_all.jsonl           (ProductRecord, 라벨 슬롯 하나당 한 줄)
       data/processed/musinsa_snap_normalization_report.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.normalization.musinsa_snap import (  # noqa: E402
    SnapNormalizer,
    find_snapshot_dir,
    iter_slots,
    snap_report,
)

OUT = ROOT / "data" / "processed"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    norm = SnapNormalizer()
    records = [norm.normalize_slot(s) for s in iter_slots(find_snapshot_dir())]
    report = snap_report(records)
    (OUT / "musinsa_snap_normalization_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "musinsa_snap_all.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("by_category_minor",)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
