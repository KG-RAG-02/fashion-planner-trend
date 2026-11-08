#!/usr/bin/env python3
"""Calibrate once from Codex visual receipts and publish a separate sidecar."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path

from catalog_content import MODEL, REVISION, atomic_json, inputs, now, read_jsonl, sha


def classify(score, low, high):
    return "non_garment" if score <= low else "garment" if score >= high else "uncertain"


def wilson(k, n):
    if not n:
        return None
    z = 1.959963984540054
    center = (k / n + z * z / (2 * n)) / (1 + z * z / n)
    delta = z * math.sqrt(k / n * (1 - k / n) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0, center - delta), min(1, center + delta)]


def confusion(review, low, high):
    matrix = {p: {a: 0 for a in ("garment", "non_garment", "unclear")}
              for p in ("garment", "non_garment", "uncertain")}
    for r in review:
        matrix[classify(r["garment_score"], low, high)][r["codex_label"]] += 1
    precisions = {}
    for pred in ("garment", "non_garment"):
        n = sum(matrix[pred].values())
        k = matrix[pred][pred]
        precisions[pred] = dict(correct=k, predicted=n, estimate=k / n if n else None,
                                wilson_95=wilson(k, n))
    return dict(matrix=matrix, precision=precisions)


def weighted_precision(review, low, high):
    sums = {k: [0., 0.] for k in ("garment", "non_garment")}
    for r in review:
        pred = classify(r["garment_score"], low, high)
        if pred in sums:
            w = r["population_stratum_n"] / r["sample_stratum_n"]
            sums[pred][1] += w
            sums[pred][0] += w * (r["codex_label"] == pred)
    return {k: dict(estimated_correct=a, estimated_predictions=b,
                   precision_estimate=a / b if b else None) for k, (a, b) in sums.items()}


def main():
    import pyarrow as pa
    import pyarrow.parquet as pq
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", required=True, type=Path)
    args = parser.parse_args()
    shared = args.shared.resolve()
    p = shared / "preprocess"
    image_dir = shared / "images"
    initial = json.loads((p / "content_initial_thresholds.json").read_text())
    sample = read_jsonl(p / "content_review_sample.jsonl")
    labels = read_jsonl(p / "content_codex_review.jsonl")
    labels = {r["url_key"]: r for r in labels}
    assert len(sample) >= 120
    assert all(r["url_key"] in labels for r in sample), "Missing direct visual reviews"
    review = []
    for s in sample:
        r = {**s, **labels[s["url_key"]]}
        assert r["codex_label"] in {"garment", "non_garment", "unclear"}
        assert r["codex_reason"] and r["viewed_original"] is True
        review.append(r)
    calibration = [r for r in review if r["review_partition"] == "calibration"]
    holdout = [r for r in review if r["review_partition"] == "holdout"]
    thresholds_path = p / "content_final_thresholds.json"
    fingerprint = {"sample_sha256": sha(p / "content_review_sample.jsonl"),
                   "reviews_sha256": sha(p / "content_codex_review.jsonl")}
    if thresholds_path.exists():
        thresholds = json.loads(thresholds_path.read_text())
        assert thresholds["fingerprint"] == fingerprint, "Already adjusted once; do not retune"
    else:
        ordered = sorted(calibration, key=lambda r: r["garment_score"])
        first_nonnegative = next((r["garment_score"] for r in ordered
                                  if r["codex_label"] != "non_garment"), 1.)
        negatives = [r["garment_score"] for r in ordered
                     if r["codex_label"] == "non_garment" and r["garment_score"] < first_nonnegative]
        last_nonpositive = next((r["garment_score"] for r in reversed(ordered)
                                if r["codex_label"] != "garment"), 0.)
        positives = [r["garment_score"] for r in ordered
                     if r["codex_label"] == "garment" and r["garment_score"] > last_nonpositive]
        low = (max(negatives) + first_nonnegative) / 2 if negatives else -1.
        high = (last_nonpositive + min(positives)) / 2 if positives else 2.
        assert low < high
        thresholds = dict(at=now(), adjustment_count=1, low=low, high=high,
            method="Largest empirical zero-error lower prefix and upper suffix in calibration only; boundary is midpoint to nearest adverse label. unclear is adverse for both. No holdout tuning.",
            calibration_n=len(calibration), holdout_n=len(holdout), fingerprint=fingerprint)
        atomic_json(thresholds_path, thresholds)
    lo, hi = thresholds["low"], thresholds["high"]
    receipts = {r["url_key"]: r for r in read_jsonl(p / "catalog_fetch.jsonl")}
    scores = {r["url_key"]: r for r in read_jsonl(p / "catalog_scores.jsonl")}
    products, urls = inputs(shared)
    assert set(urls) == set(receipts)
    assert all(k in scores for k, r in receipts.items() if r["fetch_status"] == "ok")
    url_records = {}
    for key in urls:
        fetched, scored, label = receipts[key], scores.get(key), labels.get(key)
        okay = fetched["fetch_status"] == "ok" and scored and scored["score_status"] == "ok"
        url_records[key] = dict(url_key=key, local_path=fetched.get("local_path"),
            fetch_status=fetched["fetch_status"],
            image_content=classify(scored["garment_score"], lo, hi) if okay else "uncertain",
            top_label=scored["top_label"] if okay else None,
            scores=json.dumps(scored["scores"], ensure_ascii=False, sort_keys=True) if okay else None,
            garment_score=scored["garment_score"] if okay else None,
            model=MODEL, model_revision=REVISION, codex_review=bool(label),
            codex_label=label["codex_label"] if label else None,
            codex_reason=label["codex_reason"] if label else None,
            source="vision", approx=True)
    output = []
    import hashlib
    for product in products:
        key = hashlib.sha256(product["image_ref"].encode()).hexdigest()[:16]
        output.append(dict(product_id=product["product_id"], **url_records[key]))
    table = pa.Table.from_pylist(output)
    temp = image_dir / "content.parquet.tmp"
    pq.write_table(table, temp, compression="zstd")
    temp.replace(image_dir / "content.parquet")
    stats = dict(at=now(), product_rows=len(output), unique_urls=len(urls),
        distribution_products=dict(Counter(r["image_content"] for r in output)),
        distribution_urls=dict(Counter(r["image_content"] for r in url_records.values())),
        fetch_status=dict(Counter(r["fetch_status"] for r in receipts.values())),
        reviewed_urls=len(review), reviewed_product_rows=sum(r["codex_review"] for r in output),
        codex_labels=dict(Counter(r["codex_label"] for r in review)),
        initial=confusion(review, initial["low"], initial["high"]),
        final_all=confusion(review, lo, hi), final_holdout=confusion(holdout, lo, hi),
        weighted_final=weighted_precision(review, lo, hi), thresholds=thresholds,
        index_sha256=sha(image_dir / "index.parquet"), content_sha256=sha(image_dir / "content.parquet"),
        input_sha256=sha(shared / "data/processed/v1/otpensource_all.jsonl"))
    atomic_json(p / "content_validation_stats.json", stats)
    report = ["# 카탈로그 이미지 내용 판정 — C2 2단계 A", "",
        f"생성 {stats['at']}. 원본 인덱스 열·데이터는 변경하지 않았다. 별도 `content.parquet` {len(output):,}행, 고유 URL {len(urls):,}개.", "",
        "## 분포", "", "| image_content | 상품 행 | URL |", "|---|---:|---:|"]
    for k in ("garment", "non_garment", "uncertain"):
        report.append(f"| {k} | {stats['distribution_products'].get(k,0)} | {stats['distribution_urls'].get(k,0)} |")
    report += ["", f"GET 상태(URL): `{stats['fetch_status']}`. 원본 GET entity bytes 저장; SHA256·크기·원본 해상도 영수증=`preprocess/catalog_fetch.jsonl`. HEAD·Range 없음, 8동시·10초·재시도2·개인정보 없는 UA.", "",
        "## 모델·문턱값", "",
        f"무료 로컬 `{MODEL}` @ `{REVISION}`. CPU/MPS 실제 실행 근거=`preprocess/content_model.json`. [고정 모델 카드](https://huggingface.co/{MODEL}/blob/{REVISION}/README.md)는 license 필드를 선언하지 않는다. [공식 CLIP 라이선스](https://github.com/openai/CLIP/blob/main/LICENSE)는 MIT. 이 차이를 라이선스 메타데이터에 그대로 기록했다.", "",
        "9개 프롬프트별 softmax 확률을 `scores` JSON에 기록했다. 이 값은 모델 확률이며 LLM이 만든 점수가 아니다. `garment_score=max(옷 3개)/(max(옷 3개)+max(비의류 6개))`; 실세계 정답 확률로 보정된 값은 아니다. 프롬프트 전체는 `content_model.json`에 있다.", "",
        f"표본 전 임시 구간: low={initial['low']:.9f}, high={initial['high']:.9f}. 전체 점수의 |g-0.5| 하위 20%를 uncertain으로 두는 분포 기반 규칙이다(0.5는 두 가족 argmax 경계). 표본 층 `{initial['sample']}`, seed={initial['seed']}.", "",
        f"최종 **1회 조정**: g≤{lo:.9f} → non_garment; g≥{hi:.9f} → garment; 사이 → uncertain. 임의 수치 고정 대신 calibration {len(calibration)}장의 양 끝에서 관측 오분류가 없는 최대 연속 구간을 택하고 인접 반대 판정과 중간값을 사용했다. unclear는 양쪽에서 오분류로 취급했다. 표본 선정 때 분리한 holdout {len(holdout)}장은 문턱 결정에 미사용. 이후 재조정하지 않았다.", "",
        "## 직접 검수·혼동행렬", "",
        f"`view_image`로 원본 {len(review)}장 직접 확인. 한 줄 근거=`preprocess/content_codex_review.jsonl`; 표본·층·분할=`content_review_sample.jsonl`. 검수 라벨 `{stats['codex_labels']}`. 동일 URL 상품에는 같은 판정을 전파했다."]
    for key, title in [("initial", "임시 구간 전체 표본"), ("final_all", "최종 구간 전체 표본"), ("final_holdout", "최종 구간 holdout")]:
        result = stats[key]
        report += ["", f"### {title}", "", "행=모델, 열=Codex 직접 검수.", "", "| 모델 | garment | non_garment | unclear |", "|---|---:|---:|---:|"]
        for pred, row in result["matrix"].items():
            report.append(f"| {pred} | {row['garment']} | {row['non_garment']} | {row['unclear']} |")
        for pred, val in result["precision"].items():
            if val["predicted"]:
                a, b = val["wilson_95"]
                report.append(f"- {pred} 표본 정밀도 {val['correct']}/{val['predicted']}={val['estimate']:.1%}; Wilson 95% 구간 {a:.1%}–{b:.1%}.")
            else:
                report.append(f"- {pred}: 해당 예측 표본 없음 → 정밀도 추정 불가.")
    report += ["", "층별 N/n 가중(전체 표본) 정밀도 추정: `" + json.dumps(stats["weighted_final"], ensure_ascii=False) + "`. 층을 과표집했으므로 단순 전체 정확도를 모집단 정확도로 제시하지 않는다. 전체 표본에는 calibration이 포함되므로 낙관 편향이 있다. holdout도 작고 동일 데이터/동일 검수자라 독립 사람 gold 평가가 아니다.", "",
        "## 옷이 아닌 사진 예시(직접 확인)", ""]
    non = [r for r in review if r["codex_label"] == "non_garment"]
    for r in non[:10]:
        report.append(f"- `{r['local_path']}` — {r['codex_reason']}")
    report += ["", "## 사용·한계", "",
        "`product_id`로 left join. URL 단위 결과가 상품별 반복된다. `codex_review=false`면 직접 본 이미지가 아니다. image_content는 최종 모델 판정이고 codex_label은 별도 검수값이며 덮어쓰지 않았다. `source=vision`, `approx=true`. local_path는 로컬 sidecar 전용이고 Product·결과·커밋에 포함하지 않는다.", "",
        "완성 의류가 보이면 로고·부분 확대가 함께 있어도 garment. 직물/단추/라벨만 확대되고 의류 형태가 식별되지 않으면 non_garment. 경계 사례는 unclear. CLIP은 프롬프트·센터링·텍스트의 영향을 받으며 디테일 사진과 전체 의류를 혼동할 수 있다. 모델 입력은 메모리의 224×224 전체 이미지 텐서(센터 크롭 꺼짐)이며 원본 저장 바이트에 리사이즈·크롭·재인코딩을 하지 않았다. 대표 사진 한 장만 평가해 상품 자체의 의류 여부나 세트 포함 범위를 확정하지 않는다.", "",
        "L0 채택 전 계약은 불변이다. 채택하더라도 non_garment는 이미지 표시 우선순위만 낮추고 상품 후보·점수·필터에서 상품을 제외하지 않는다. 원본·캐시·분석은 내부 연구용으로 git에 넣지 않는다.", "",
        f"검증 해시: index `{stats['index_sha256']}`; content `{stats['content_sha256']}`. 상세 통계=`preprocess/content_validation_stats.json`."]
    (image_dir / "content_report.md").write_text("\n".join(report) + "\n")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
