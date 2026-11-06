"""02 otpensource를 표준 어휘로 정규화해 **전체**를 저장한다 (03은 build_musinsa_snap.py, 같은 AliasMap).

실행:  python scripts/build_otpensource.py
출력:  data/processed/otpensource_all.jsonl                   (ProductRecord, 한 줄에 하나. 그래프 적재용)
       data/processed/otpensource_normalization_report.json   (매핑 결과 요약)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.normalization.otpensource import (  # noqa: E402
    OtpenNormalizer,
    find_snapshot_dir,
    iter_rows,
    normalization_report,
)

OUT = ROOT / "data" / "processed"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = list(iter_rows(find_snapshot_dir()))
    norm = OtpenNormalizer()
    records = [norm.normalize_row(r) for r in rows]

    report = normalization_report(records, rows)
    (OUT / "otpensource_normalization_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "otpensource_all.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
