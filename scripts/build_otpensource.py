"""02 otpensource를 표준 어휘로 정규화해 **전체**를 저장한다 (03은 build_musinsa_snap.py, 같은 AliasMap).

실행:  python scripts/build_otpensource.py [--out DIR] [--snapshot-dir DIR] [--image-index PATH]   # --out 기본 data/processed/v2
       --snapshot-dir를 주지 않으면 HF 캐시(HF_HOME)의 고정 리비전 스냅샷을 쓴다(없으면 내려받는다).
출력(--out):
  otpensource_all.jsonl                 ProductRecord 한 줄에 하나. 세트 분해 구성품 포함, 분해된 부모는 빠진다.
                                        image_url이 없는 행(원천의 빈 행)은 저장하지 않는다(D-33). 그래프 적재는 통합본
                                        musinsa_dataset_v2.jsonl(build_musinsa_dataset.py)로 한다
  otpensource_normalization_report.json 매핑 결과 요약(+ 분해·팩·후보·빈 행·dup_of·이미지 상태)
  otpensource_split_parents.jsonl       분해된 부모 원본 레코드와 구성품 id
  otpensource_split_review.csv          세트·팩 표지가 있는 모든 행의 판정(사람 검토용)
  BUILD_INFO.json                       입력 sha256·리비전·커밋
  --review-candidates PATH              세트 후보 목록(product_id, name, rule_result …) — 세트 검토 입력
읽기 전용으로 지정된 폴더(`output.protected_dirs`)와 심볼릭 링크 대상에는 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import os
import csv
import io
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.normalization.image_index import ImageJoiner, load_image_index  # noqa: E402
from trend_agent.ingestion.normalization.otpensource import (  # noqa: E402
    REPO_ID,
    REVISION,
    OtpenNormalizer,
    _clean,
    find_snapshot_dir,
    iter_rows,
    drop_without_image,
    mark_exact_duplicates,
    normalization_report,
)
from trend_agent.ingestion.normalization.output import (  # noqa: E402
    git_state,
    resolve_out,
    sha256_file,
    update_build_info,
    write_json_atomic,
    write_lines_atomic,
    write_text_atomic,
)
from trend_agent.ingestion.normalization.set_split import (  # noqa: E402
    SetOverrides,
    SetSplitter,
    apply_split,
    review_row,
)

CANDIDATE_FIELDS = ["product_id", "name", "rule_result", "reason", "decision_source", "sub_category", "components", "accessories",
                    "pack_size", "image_url", "image_status", "component_ids"]
RULE_RESULT = {"split": "split", "candidate": "candidate", "multipack": "multipack", "accessory_set": "accessory"}
REVIEW_FIELDS = ["id", "name", "sub_category", "decision", "reason", "decision_source", "components", "accessories", "pack_size",
                 "markers", "parent_major", "parent_major_mismatch"]


def build(snapshot_dir: Path, out: Path, image_index: Path | None, review_candidates: Path | None = None) -> dict:
    rows = list(iter_rows(snapshot_dir))
    norm = OtpenNormalizer()
    records = mark_exact_duplicates([norm.normalize_row(r) for r in rows], rows)

    splitter = SetSplitter()
    overrides = SetOverrides()
    body, parents, review = [], [], []
    decisions: Counter = Counter()
    for rec, row in zip(records, rows):
        name = _clean(row.get("product_name"))
        rule = splitter.decide(name)
        d = overrides.apply(rec.node.id, rule)
        decisions[d.decision] += 1
        if d is not rule:
            decisions[f"review:{rule.decision}->{d.decision}"] += 1
        if d.decision != "none" or d is not rule:  # 검수로 none이 된 행(표시 해제)도 리뷰 목록에 남긴다
            major = rec.category_major.value if rec.category_major else None
            review.append(review_row(rec.node.id, name, _clean(row.get("sub_category")), major, d))
        out_recs = apply_split(rec, d, norm, row)
        if d.decision == "split":
            parents.append({"id": rec.node.id, "decision": d.decision, "reason": d.reason,
                            "component_ids": [c.node.id for c in out_recs],
                            "accessories": d.accessories, "record": json.loads(rec.model_dump_json())})
        body.extend(out_recs)
    body, drop_stats = drop_without_image(body)  # D-33: 이미지 URL 없는 행은 저장하지 않는다

    joiner = ImageJoiner(load_image_index(image_index) if image_index else None)
    for rec in body:
        joiner.apply(rec)

    report = normalization_report(body, rows)
    report["split_decisions"] = dict(decisions)
    report["image_join"] = dict(joiner.stats)
    report["image_ref_rows"] = sum(1 for r in body if r.node.image_ref)
    report.update(drop_stats)

    write_lines_atomic(out / "otpensource_all.jsonl", (r.model_dump_json() for r in body))
    write_json_atomic(out / "otpensource_normalization_report.json", report)
    write_lines_atomic(out / "otpensource_split_parents.jsonl", (json.dumps(p, ensure_ascii=False) for p in parents))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REVIEW_FIELDS)
    w.writeheader()
    w.writerows(review)
    write_text_atomic(out / "otpensource_split_review.csv", buf.getvalue())
    if review_candidates:
        by_id = {r.node.parent_id or r.node.id: r for r in body}
        comp_ids: dict[str, list[str]] = {}
        for r in body:
            if r.node.parent_id:
                comp_ids.setdefault(r.node.parent_id, []).append(r.node.id)
        rows_out = []
        for rv in review:
            rec = by_id.get(rv["id"])
            rows_out.append({
                "product_id": rv["id"], "name": rv["name"], "rule_result": RULE_RESULT.get(rv["decision"], rv["decision"]),
                "reason": rv["reason"], "decision_source": rv["decision_source"], "sub_category": rv["sub_category"], "components": rv["components"],
                "accessories": rv["accessories"], "pack_size": rv["pack_size"],
                "image_url": rec.node.image_url if rec else "", "image_status": (rec.node.image_status or "") if rec else "",
                "component_ids": " | ".join(comp_ids.get(rv["id"], [])),
            })
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CANDIDATE_FIELDS)
        w.writeheader()
        w.writerows(rows_out)
        review_candidates.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(review_candidates, buf.getvalue())

    files = sorted(Path(snapshot_dir).rglob("*.json"))
    update_build_info(out, "otpensource", {
        "mode": "raw",
        "repo_id": REPO_ID,
        "revision": REVISION,
        "snapshot_dir_files": len(files),
        "input_sha256": {str(f.relative_to(snapshot_dir)): sha256_file(f) for f in files},
        "image_index": str(image_index) if image_index else None,
        "image_index_sha256": sha256_file(image_index) if image_index else None,
        "git": git_state(ROOT),
        "outputs": {n: sha256_file(out / n) for n in ("otpensource_all.jsonl", "otpensource_normalization_report.json",
                                                      "otpensource_split_parents.jsonl", "otpensource_split_review.csv")},
        "rows_raw": len(rows), "rows_out": len(body), "dropped_no_image_url": drop_stats["dropped_no_image_url"],
    })
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "data" / "processed" / "v2"), help="출력 폴더 (기본 data/processed/v2)")
    ap.add_argument("--snapshot-dir", help="02 원천 JSON 폴더(HF 스냅샷). 없으면 HF 캐시의 고정 리비전")
    ap.add_argument("--image-index", help="이미지 인덱스 parquet (선택. 없으면 이미지 상태를 채우지 않는다)")
    ap.add_argument("--review-candidates", help="세트 후보 CSV를 쓸 경로 (선택)")
    a = ap.parse_args()
    out = resolve_out(a.out)
    snap = Path(a.snapshot_dir) if a.snapshot_dir else find_snapshot_dir()
    report = build(snap, out, Path(a.image_index) if a.image_index else None,
                   Path(a.review_candidates) if a.review_candidates else None)
    print(json.dumps({k: v for k, v in report.items() if k != "by_category_major"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
