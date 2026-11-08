"""03 musinsa-snap-new를 표준 어휘로 정규화해 전체를 저장한다 (02와 같은 AliasMap).

실행:  python scripts/build_musinsa_snap.py --out "$SHARED/data/processed/v2" [--parquet PATH] [--image-index PATH]
       --parquet을 주지 않으면 C2가 받은 고정 리비전 $SHARED/hf/data/train-00000-of-00001.parquet, 없으면 HF 캐시($SHARED/hf_cache)
출력(--out):
  musinsa_snap_all.jsonl                 ProductRecord, 라벨 슬롯 하나당 한 줄(슬롯 수·id는 v1과 같다)
  musinsa_snap_normalization_report.json
  BUILD_INFO.json                        입력 sha256·리비전·커밋
v1 폴더와 심볼릭 링크 대상에는 쓰지 않는다. 이미지 바이트는 읽지 않는다(label 열만).
"""
from __future__ import annotations

import argparse
import os
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if os.environ.get("SHARED"):  # 원천 캐시는 팀 공유 위치에 둔다(사용자 기본 HF 캐시에 받지 않는다)
    os.environ.setdefault("HF_HOME", str(Path(os.environ["SHARED"]) / "hf_cache"))

from trend_agent.ingestion.normalization.image_index import ImageJoiner, load_image_index  # noqa: E402
from trend_agent.ingestion.normalization.musinsa_snap import (  # noqa: E402
    REPO_ID,
    REVISION,
    SnapNormalizer,
    find_snapshot_dir,
    iter_slots,
    mark_snap_duplicates,
    scalar_list_fields,
    snap_report,
)
from trend_agent.ingestion.normalization.output import (  # noqa: E402
    git_state,
    resolve_out,
    sha256_file,
    update_build_info,
    write_json_atomic,
    write_lines_atomic,
)


def build(parquet: Path, out: Path, image_index: Path | None) -> dict:
    slots = list(iter_slots(parquet))
    norm = SnapNormalizer()
    records = mark_snap_duplicates([norm.normalize_slot(s) for s in slots], slots)
    joiner = ImageJoiner(load_image_index(image_index) if image_index else None)
    for rec in records:
        joiner.apply(rec)

    report = snap_report(records)
    list_fields: Counter = Counter()
    for s in slots:
        list_fields.update(scalar_list_fields(s["attrs"]))
        for k, v in s["attrs"].items():
            if isinstance(v, (list, tuple)):
                list_fields[f"list:{k}"] += 1
    report["list_values"] = dict(list_fields)
    report["image_join"] = dict(joiner.stats)
    report["image_status"] = dict(Counter(str(r.node.image_status) for r in records))
    report["multi_wearer_suspected"] = sum(1 for r in records if r.node.multi_wearer_suspected)

    write_lines_atomic(out / "musinsa_snap_all.jsonl", (r.model_dump_json() for r in records))
    write_json_atomic(out / "musinsa_snap_normalization_report.json", report)
    update_build_info(out, "musinsa_snap", {
        "mode": "raw",
        "repo_id": REPO_ID,
        "revision": REVISION,
        "input": str(parquet),
        "input_sha256": sha256_file(parquet) if parquet.is_file() else None,
        "image_index": str(image_index) if image_index else None,
        "image_index_sha256": sha256_file(image_index) if image_index else None,
        "git": git_state(ROOT),
        "outputs": {n: sha256_file(out / n) for n in ("musinsa_snap_all.jsonl", "musinsa_snap_normalization_report.json")},
        "slots": len(records),
    })
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="출력 폴더(예: $SHARED/data/processed/v2). v1 폴더는 거부")
    ap.add_argument("--parquet", help="03 원천 parquet 파일 또는 폴더. 없으면 HF 캐시")
    ap.add_argument("--image-index", help="C2 이미지 인덱스 parquet($SHARED/images/index.parquet)")
    a = ap.parse_args()
    out = resolve_out(a.out)
    pinned = Path(os.environ.get("SHARED", "")) / "hf" / "data" / "train-00000-of-00001.parquet"
    parquet = Path(a.parquet) if a.parquet else (pinned if pinned.is_file() else find_snapshot_dir())
    report = build(parquet, out, Path(a.image_index) if a.image_index else None)
    print(json.dumps({k: v for k, v in report.items() if k not in ("by_category_minor", "category_null_raw",
                                                                   "category_major_only_raw")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
