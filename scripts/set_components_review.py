#!/usr/bin/env python3
"""Prepare/finalize human-readable Codex visual set reviews; never splits products.

Visual judgments are supplied explicitly via JSON on stdin to `record`, after
view_image. No model/API calls or name-rule-generated visual labels occur here.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import csv
import hashlib
from enum import Enum
import json
import math
from pathlib import Path
import random
import shutil
import sys

from catalog_content import append_jsonl, atomic_json, now, read_jsonl, sha

# Compile only the authoritative enum/mapping definitions. Review receipts need
# no pydantic or unrelated schema imports, so all phases stay standard-library
# only even when the shared environment is being used by other workers.
_vocabulary_path = Path(__file__).resolve().parents[1] / "src/trend_agent/schemas/vocabulary.py"
_tree = ast.parse(_vocabulary_path.read_text())
_definitions = [node for node in _tree.body if
    (isinstance(node, ast.ClassDef) and node.name in {"Color", "CategoryMajor", "CategoryMinor"})
    or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        and node.target.id == "MINOR_TO_MAJOR")]
_namespace = {"Enum": Enum}
exec(compile(ast.Module(body=_definitions, type_ignores=[]), str(_vocabulary_path), "exec"), _namespace)
CategoryMajor, CategoryMinor = _namespace["CategoryMajor"], _namespace["CategoryMinor"]
Color, MINOR_TO_MAJOR = _namespace["Color"], _namespace["MINOR_TO_MAJOR"]

KINDS = {"multi_type_set", "multipack", "single", "accessory_set", "unclear"}
CONFIDENCES = {"high", "medium", "low"}


def prepare(shared, t2):
    p = shared / "preprocess"
    queue = p / "set_review_queue.jsonl"
    if queue.exists():
        print("Frozen queue already exists; reusing it")
        return
    candidate = p / "set_review_candidates.csv"
    products = read_jsonl(shared / "data/processed/v1/otpensource_all.jsonl")
    by_id = {r["node"]["id"]: r for r in products}
    if candidate.exists():
        source = "T2:set_review_candidates.csv"
        snapshot = p / "set_review_candidates.input.csv"
        shutil.copyfile(candidate, snapshot)
        candidates = list(csv.DictReader(snapshot.open()))
    else:
        import re
        source = "fallback:v1_name_markers"
        marker = re.compile(r"셋업|세트|투피스|상하의|(?<![a-z])set(?:up)?(?![a-z])|[2-9]\s*-?\s*(?:pack|팩)|1\s*\+\s*1", re.I)
        candidates = []
        for r in products:
            n = r["node"]
            if marker.search(n.get("name") or ""):
                candidates.append(dict(product_id=n["id"], name=n["name"],
                    image_url=n.get("image_url"), rule_result="", sub_category=n.get("category_raw")))
        snapshot = None
    receipts = {r["url_key"]: r for r in read_jsonl(p / "catalog_fetch.jsonl")}
    queue_rows = []
    for i, c in enumerate(candidates, 1):
        product = by_id[c["product_id"]]
        node = product["node"]
        url = c.get("image_url") or node.get("image_url")
        key = hashlib.sha256(url.encode()).hexdigest()[:16] if url else None
        fetch = receipts.get(key, {})
        queue_rows.append(dict(review_id=i, product_id=c["product_id"], name=node["name"],
            category_raw=node.get("category_raw"), sub_category=c.get("sub_category"),
            image_url=url, url_key=key, local_path=fetch.get("local_path"),
            image_status=c.get("image_status"), fetch_status=fetch.get("fetch_status", "not_in_A_ok_scope"),
            image_sha256=fetch.get("sha256"), candidate_source=source,
            rule_result=c.get("rule_result"), rule_reason=c.get("reason"),
            rule_components=c.get("components"), rule_accessories=c.get("accessories"),
            pack_size=c.get("pack_size"), component_ids=c.get("component_ids")))
    assert len({r["product_id"] for r in queue_rows}) == len(queue_rows)
    append_jsonl(queue, queue_rows)
    fingerprints = {}
    for rel in ["configs/alias_map/set_components.yaml", "src/trend_agent/ingestion/normalization/set_split.py"]:
        path = t2 / rel
        if path.exists():
            fingerprints[rel] = sha(path)
    atomic_json(p / "set_review_input.json", dict(at=now(), candidate_source=source,
        candidate_rows=len(queue_rows), candidate_sha256=sha(candidate) if candidate.exists() else None,
        snapshot_sha256=sha(snapshot) if snapshot else None, queue_sha256=sha(queue),
        v1_sha256=sha(shared / "data/processed/v1/otpensource_all.jsonl"),
        rule_counts=dict(Counter(r["rule_result"] for r in queue_rows)),
        unavailable_images=[r["review_id"] for r in queue_rows if not r["local_path"]],
        t2_read_only_fingerprints=fingerprints))
    print(json.dumps(dict(rows=len(queue_rows), images=sum(bool(r["local_path"]) for r in queue_rows),
        missing=[r["review_id"] for r in queue_rows if not r["local_path"]]), ensure_ascii=False))


def record(shared, repeat=False):
    p = shared / "preprocess"
    queue = {r["review_id"]: r for r in read_jsonl(p / "set_review_queue.jsonl")}
    target = p / ("set_consistency_rereview.jsonl" if repeat else "set_components_review.jsonl")
    seen = {r["review_id"] for r in read_jsonl(target)}
    annotations = json.load(sys.stdin)
    rows = []
    for a in annotations:
        q = queue[a["review_id"]]
        assert a["review_id"] not in seen
        seen.add(a["review_id"])
        assert a["kind"] in KINDS and a["confidence"] in CONFIDENCES
        assert a["reason"]
        components = []
        for c in a["components"]:
            minor = c.get("category_minor")
            if minor is not None:
                minor_enum = CategoryMinor(minor)
                major = MINOR_TO_MAJOR[minor_enum].value
            else:
                major = c.get("category_major")
                if major is not None:
                    CategoryMajor(major)
            colors = [dict(value=Color(color).value, source="vision", approx=True)
                      for color in c.get("colors", [])]
            assert not colors or q["local_path"], "No pixels: no vision colors"
            ev = c["evidence"]
            assert ev.get("name_tokens") is not None and ev.get("visible")
            for token in ev["name_tokens"]:
                assert token.casefold() in q["name"].casefold(), f"Absent name token {token}"
            components.append(dict(category_major=major, category_minor=minor,
                                   colors=colors, evidence=ev))
        row = {**q, "kind": a["kind"], "components": components,
               "confidence": a["confidence"], "reason": a["reason"],
               "viewed_original": bool(q["local_path"]),
               "review_tool": "view_image" if q["local_path"] else None,
               "source": "vision" if q["local_path"] else None,
               "approx": True, "reviewed_at": now()}
        if not q["local_path"]:
            row["image_limitation"] = "No A cache: input image_status broken; name/category evidence only"
        rows.append(row)
    append_jsonl(target, rows)
    print("saved", [r["review_id"] for r in rows], "total", len(seen))


def repeat_sample(shared):
    p = shared / "preprocess"
    target = p / "set_consistency_sample.json"
    if target.exists():
        print(target.read_text())
        return
    queue = read_jsonl(p / "set_review_queue.jsonl")
    reviews = read_jsonl(p / "set_components_review.jsonl")
    assert {r["product_id"] for r in queue} == {r["product_id"] for r in reviews}
    rng = random.Random(20261109)
    chosen = sorted(rng.sample([r["review_id"] for r in queue], math.ceil(len(queue) / 10)))
    obj = dict(seed=20261109, rows=len(queue), n=len(chosen), fraction=.1, review_ids=chosen,
               method="uniform random sample without replacement after first pass; rereview with name/category/image only")
    atomic_json(target, obj)
    print(json.dumps(obj))


def signature(row):
    def comp(c):
        return (c["category_major"], c["category_minor"],
                tuple(sorted(x["value"] for x in c["colors"])))
    return row["kind"], tuple(sorted((comp(c) for c in row["components"]), key=repr))


def compare_v2(shared, reviews):
    """Read T2 outputs without modifying source products or rule files."""
    p = shared / "preprocess"
    v2 = shared / "data/processed/v2"
    paths = [v2 / "otpensource_all.jsonl", v2 / "otpensource_split_parents.jsonl",
             v2 / "otpensource_split_review.csv"]
    if not all(path.exists() for path in paths):
        result = dict(available=False, missing=[str(path) for path in paths if not path.exists()])
        atomic_json(p / "set_v2_comparison.json", result)
        return result
    products = read_jsonl(paths[0])
    by_id = {r["node"]["id"]: r for r in products}
    parents = {r["id"]: r for r in read_jsonl(paths[1])}
    children = {}
    for row in products:
        parent_id = row["node"].get("parent_id")
        if parent_id:
            children.setdefault(parent_id, []).append(row)
    current_candidates = list(csv.DictReader(paths[2].open(encoding="utf-8-sig")))
    rows = []
    for r in reviews:
        pid = r["product_id"]
        child = children.get(pid, [])
        expected = parents.get(pid, {}).get("component_ids", [])
        original = by_id.get(pid, {}).get("node", {})
        categories = sorted([(c["category_major"], c["node"].get("category_minor"))
                             for c in child], key=repr)
        reviewed = sorted([(c["category_major"], c["category_minor"])
                           for c in r["components"]], key=repr)
        row = dict(product_id=pid, review_id=r["review_id"], name=r["name"],
            rule_result=r["rule_result"], kind=r["kind"], confidence=r["confidence"],
            original_in_v2=pid in by_id, split_parent_present=pid in parents,
            v2_split_candidate=original.get("split_candidate"),
            v2_multipack=original.get("multipack"), child_ids=[c["node"]["id"] for c in child],
            child_categories=categories, reviewed_categories=reviewed,
            component_categories_match=(categories == reviewed) if child else None,
            child_ids_match_parent=(set(expected) == {c["node"]["id"] for c in child}) if expected else None)
        assert row["split_parent_present"] == (r["rule_result"] == "split")
        assert row["original_in_v2"] == (r["rule_result"] != "split")
        if r["rule_result"] == "split":
            assert row["child_ids_match_parent"]
        elif r["rule_result"] == "candidate":
            assert row["v2_split_candidate"] is True
        elif r["rule_result"] == "multipack":
            assert row["v2_multipack"] is True
        rows.append(row)
    assert {r["id"] for r in current_candidates} == {r["product_id"] for r in reviews}
    target = p / "set_v2_comparison.csv"
    with target.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
                             for k, v in row.items()})
    result = dict(available=True, rows=len(rows), source_sha256={path.name: sha(path) for path in paths},
        matched_candidate_ids=len(rows), split_parents=len(parents), split_children=sum(map(len, children.values())),
        split_category_exact_matches=sum(r["component_categories_match"] is True for r in rows),
        split_category_differences=[r for r in rows if r["component_categories_match"] is False],
        candidate_high_multi_type=sum(r["rule_result"] == "candidate" and
            r["kind"] == "multi_type_set" and r["confidence"] == "high" for r in reviews),
        csv=str(target), source_files_read_only=True)
    atomic_json(p / "set_v2_comparison.json", result)
    return result


def finalize(shared):
    p = shared / "preprocess"
    queue = read_jsonl(p / "set_review_queue.jsonl")
    reviews = read_jsonl(p / "set_components_review.jsonl")
    by_id = {r["product_id"]: r for r in reviews}
    assert len(by_id) == len(reviews) == len(queue)
    reviews = [by_id[r["product_id"]] for r in queue]
    # Rewrite in frozen input order, preserving the explicit decisions.
    temp = p / "set_components_review.jsonl.tmp"
    temp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reviews))
    temp.replace(p / "set_components_review.jsonl")
    csv_path = p / "set_components_review.csv"
    fields = ["product_id", "name", "category_raw", "local_path", "kind", "components",
              "confidence", "reason", "candidate_source", "rule_result", "viewed_original", "source", "approx"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in reviews:
            writer.writerow({**r, "components": json.dumps(r["components"], ensure_ascii=False)})
    sample = json.loads((p / "set_consistency_sample.json").read_text())
    again = read_jsonl(p / "set_consistency_rereview.jsonl")
    first = {r["review_id"]: r for r in reviews}
    assert len(again) == sample["n"] and {r["review_id"] for r in again} == set(sample["review_ids"])
    disagreements = []
    matches = Counter()
    for r in again:
        old = first[r["review_id"]]
        matches["kind"] += old["kind"] == r["kind"]
        old_categories = sorted([(c["category_major"], c["category_minor"]) for c in old["components"]], key=repr)
        new_categories = sorted([(c["category_major"], c["category_minor"]) for c in r["components"]], key=repr)
        matches["component_categories"] += old_categories == new_categories
        matches["kind_components_colors"] += signature(old) == signature(r)
        matches["confidence"] += old["confidence"] == r["confidence"]
        if signature(old) != signature(r) or old["confidence"] != r["confidence"]:
            disagreements.append(dict(review_id=r["review_id"], product_id=r["product_id"],
                first=dict(kind=old["kind"], components=old["components"], confidence=old["confidence"]),
                repeat=dict(kind=r["kind"], components=r["components"], confidence=r["confidence"])))
    consistency = dict(n=sample["n"], seed=sample["seed"], matches=dict(matches),
        rates={k: v / sample["n"] for k, v in matches.items()}, disagreements=disagreements,
        limitation="Same Codex session; original decisions hidden in rereview tool display, but this is not an independent blind human study")
    atomic_json(p / "set_consistency_results.json", consistency)
    combinations = Counter(" + ".join(sorted(c["category_minor"] or c["category_major"] or "null"
                                  for c in r["components"])) or "없음" for r in reviews if r["kind"] == "multi_type_set")
    mapping = {"split": "multi_type_set", "multipack": "multipack", "accessory": "accessory_set"}
    rule_matrix = {k: dict(Counter(r["kind"] for r in reviews if r["rule_result"] == k))
                   for k in sorted({r["rule_result"] for r in reviews})}
    diffs = [r for r in reviews if r["rule_result"] in mapping and mapping[r["rule_result"]] != r["kind"]]
    v2_comparison = compare_v2(shared, reviews)
    difference_fields = ["product_id", "name", "rule_result", "kind", "confidence", "reason", "local_path"]
    with (p / "set_rule_differences.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=difference_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(diffs)
    summary = dict(at=now(), rows=len(reviews), kind_counts=dict(Counter(r["kind"] for r in reviews)),
        confidence_counts=dict(Counter(r["confidence"] for r in reviews)),
        high_ratio=sum(r["confidence"] == "high" for r in reviews) / len(reviews),
        original_images_viewed=sum(r["viewed_original"] for r in reviews),
        missing_image_reviews=[r["review_id"] for r in reviews if not r["viewed_original"]],
        multi_type_combinations=combinations.most_common(), rule_comparison=rule_matrix,
        rule_disagreements_excluding_candidates=len(diffs), consistency=consistency, v2_comparison=v2_comparison,
        review_sha256=sha(p / "set_components_review.jsonl"),
        candidate_current_sha256=sha(p / "set_review_candidates.csv") if (p / "set_review_candidates.csv").exists() else None)
    atomic_json(p / "set_review_stats.json", summary)
    lines = ["# 세트 구성품·색 1차 검토 — C2 2단계 B", "",
        f"{len(reviews)}행. 제공받은 후보 목록을 우선 사용했다(`set_review_candidates.input.csv` 고정 사본, `set_review_input.json` 해시). 실제 CSV는 180행=split41/candidate79/multipack43/accessory17이다. 초기 집계의 179는 이후 180으로 정정됐다. 대체 후보 추출은 사용하지 않았다.", "",
        f"직접 원본 확인 {summary['original_images_viewed']}행; A 범위 밖 broken 이미지 {len(summary['missing_image_reviews'])}행은 파일이 없어서 볼 수 없었으며 이름·category_raw만 근거로 사용했다. 해당 review_id={summary['missing_image_reviews']}; 색은 비웠다. 이미지 재가공·추가 의존성 설치·유료 API 호출·상품 데이터 수정 없음.", "",
        "## 판정 분포", "", "| kind | 건수 |", "|---|---:|"]
    for kind in sorted(KINDS):
        lines.append(f"| {kind} | {summary['kind_counts'].get(kind,0)} |")
    lines += ["", f"confidence={summary['confidence_counts']}; high 비율={summary['high_ratio']:.1%}. 숫자는 건수·집계이며 개별 LLM 확신도는 high/medium/low만 기록했다.", "",
        "## multi_type_set 구성 조합", "", "| 구성 | 건수 |", "|---|---:|"]
    for combo, n in combinations.most_common(15):
        lines.append(f"| {combo} | {n} |")
    lines += ["", "## T2 규칙과 비교", "", "규칙 사전·분해 코드는 읽기 전용으로 확인했고 지문은 set_review_input.json에 있다. v2 대조 지문·상세는 set_v2_comparison.json/.csv에 있다. split→multi_type_set, accessory→accessory_set로 비교하고, candidate는 판정 보류 상태라 오답으로 세지 않는다.", "",
        "| T2 | " + " | ".join(sorted(KINDS)) + " |", "|---|" + "---:|" * len(KINDS)]
    for rule, row in rule_matrix.items():
        lines.append("| " + rule + " | " + " | ".join(str(row.get(k,0)) for k in sorted(KINDS)) + " |")
    lines += ["", f"candidate 제외 불일치 {len(diffs)}건=`set_rule_differences.csv`. 이는 Codex 1차 의견과 규칙의 차이이며 규칙 오답 확정이 아니다. 스타일링으로 함께 보이는 옷이 판매 구성품이라는 증거는 아니므로 상품명·카테고리와 구별했다.", "",
        "## 무작위 10% 재판정", "",
        f"seed={sample['seed']}, {sample['n']}/{len(reviews)}행을 비복원 무작위 추출. 이름·category_raw·원본 이미지만 다시 표시하고 1차 라벨은 표시하지 않았다. 같은 세션이므로 완전 독립·완전 맹검이 아니다."]
    for metric, count in matches.items():
        lines.append(f"- {metric}: {count}/{sample['n']}={count/sample['n']:.1%}")
    lines += ["", "불일치 전체 목록=`set_consistency_results.json`. 1차 레코드를 재판정으로 자동 덮어쓰지 않았다."]
    for d in disagreements:
        lines.append(f"- review_id {d['review_id']}, `{d['product_id']}`: 첫 {d['first']['kind']}/{d['first']['confidence']} → 재판정 {d['repeat']['kind']}/{d['repeat']['confidence']}; 구성/색은 JSON 비교 필요.")
    if v2_comparison["available"]:
        lines += ["", "## v2 실제 분해 산출물 대조", "",
            f"후보 product_id {v2_comparison['matched_candidate_ids']}개가 v2 리뷰 CSV와 일치한다. split 부모 {v2_comparison['split_parents']}개와 구성품 {v2_comparison['split_children']}개를 확인했고 부모/자식 ID 연결이 모두 맞는다. candidate79는 원 레코드와 split_candidate=true를 유지한다. 기존 split의 구성 하분류 완전 일치={v2_comparison['split_category_exact_matches']}/41; 차이는 set_v2_comparison.json에 전부 남겼다.", "",
            f"candidate 중 multi_type_set/high는 {v2_comparison['candidate_high_multi_type']}개이며 사람 검토 우선 후보일 뿐 자동 분해 승인이 아니다. 기존 v2 색·full_set 범위는 수정하지 않았다."]
        for d in v2_comparison["split_category_differences"]:
            lines.append(f"- review_id {d['review_id']}: v2={d['child_categories']}, C2={d['reviewed_categories']}")
    lines += ["", "## 사용법·한계", "",
        "`set_components_review.jsonl`와 UTF-8 BOM CSV의 product_id로 T2 목록에 조인한다. components JSON은 표준 21 하분류·4 상분류·21색 또는 null만 사용한다. 액세서리는 의류 category를 만들지 않고 null로 두며 evidence에 보이는 종류를 적었다. colors는 전부 `{value, source: vision, approx: true}`이고 보이지 않는 구성품의 색은 추측하지 않는다. 이름 토큰과 사진 관찰은 evidence의 별도 키다.", "",
        "사진에 한 벌만 보이거나 대표 이미지가 라벨/디테일뿐이면 reason/evidence에 적었다. multipack에 기록한 구성품은 식별 가능한 종류·사진 속 색의 검토 단위이며 팩의 모든 개별 장수가 확인됐다는 뜻이 아니다. 판매 범위·선택 옵션·분리 가능 여부가 불명확하면 unclear다. high도 사용자 채택이나 사람 gold를 의미하지 않는다.", "",
        "T2는 candidate 중 multi_type_set/high 등 근거가 분명한 항목부터 사람 검토 큐로 올린다. 사진만으로 자동 split하거나 기존 split을 되돌리지 않는다. 최종 적용은 T2, 채택은 사용자다. 비전 색은 approx이므로 1차 필터·점수에 미사용. 원본·데이터·검수 산출물·개인 경로는 git에 넣지 않는다."]
    lines += ["", "## 실행·해석 기록", "",
        "A 모델 처리와 B 초기 기록은 지정 공유 venv를 사용했다. B 후반에 venv Python이 단순 print·-S -E에서도 시작 지연을 보여, 표준 라이브러리만 필요한 기록/집계를 기존 시스템 Python 3.12.8로 이어갔다(run.log). 의존성을 설치하거나 venv를 변경하지 않았다. 이후 시작이 회복되어 최종 집계·전수 검증은 지정 venv로 완료했다. 표준 어휘는 저장소 vocabulary.py의 Enum·매핑 정의만 읽는다. 지연된 기록의 완료 후 product_id/review_id 중복·누락을 전수 검사했다.", "",
        "재판정 18건 중 원본 재확인 16건, 이미지 없는 이름 검토 2건이다. kind는 판매 범위에 관한 Codex 1차 의견이며 multi_type_set은 서로 다른 의류 유형, accessory_set은 의류와 비의류 구성, multipack은 동종 묶음, single은 단품 표기, unclear는 구성 불명확을 뜻한다. null category는 액세서리 또는 의류 하분류 미확정을 evidence로 구별한다."]
    (p / "set_review_report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--t2", type=Path)
    parser.add_argument("--phase", choices=["prepare", "record", "repeat-sample", "record-repeat", "finalize"], required=True)
    args = parser.parse_args()
    if args.phase == "prepare":
        assert args.t2
        prepare(args.shared, args.t2)
    elif args.phase == "record":
        record(args.shared)
    elif args.phase == "record-repeat":
        record(args.shared, repeat=True)
    elif args.phase == "repeat-sample":
        repeat_sample(args.shared)
    else:
        finalize(args.shared)


if __name__ == "__main__":
    main()
