"""02·03 v2 Product를 하나의 통합본(`musinsa_dataset_v2.jsonl`)으로 합친다 (D-33).

- 두 출처 모두 같은 `ProductRecord` 모델로 쓰므로 키(필드명)가 같다. 출처에 없는 값은 null이다
  (예: 02의 `snap_id`, 03의 `name`·`image_url`). 합칠 때 모든 줄의 키 집합이 같은지 다시 확인한다.
- 출처 추적: `node.source`(otpensource / musinsa_snap)와 `node.source_row`(원천 파일·행)가 이미 있다.
- 이미지 주소는 `node.image_ref` 하나로 통일한다: 02 = image_url, 03 = `hf://datasets/disl/musinsa-snap-new@<rev>#row=<snap_id>`.
- 검사 실패(출처 불일치, id 중복, 키 집합 불일치)는 예외로 멈춘다. 반쯤 맞는 통합본을 쓰지 않는다.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from trend_agent.schemas.graph_schema import ProductRecord

# (출처 값, 파일 이름) — 이 순서로 쓴다
SOURCES: tuple[tuple[str, str], ...] = (("otpensource", "otpensource_all.jsonl"), ("musinsa_snap", "musinsa_snap_all.jsonl"))
OUTPUT_NAME = "musinsa_dataset_v2.jsonl"
REPORT_NAME = "musinsa_dataset_v2_report.json"


class CombineError(ValueError):
    pass


def _keys(line: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    d = json.loads(line)
    return tuple(sorted(d)), tuple(sorted(d["node"]))


def combine_products(inputs: Iterable[tuple[str, Path]]) -> tuple[list[str], dict[str, Any]]:
    """inputs = [(기대 source, JSONL 경로)…]. 반환: (통합본 줄 목록, 리포트)."""
    lines: list[str] = []
    ids: set[str] = set()
    key_sets: Counter = Counter()
    by_source: dict[str, dict[str, int]] = {}
    for source, path in inputs:
        stats: Counter = Counter()
        with Path(path).open(encoding="utf-8") as f:
            for n, raw in enumerate(f, start=1):
                if not raw.strip():
                    continue
                rec = ProductRecord.model_validate_json(raw)
                node = rec.node
                if node.source != source:
                    raise CombineError(f"{path}:{n} source={node.source!r}, 기대값 {source!r}")
                if node.id in ids:
                    raise CombineError(f"{path}:{n} id 중복: {node.id}")
                ids.add(node.id)
                line = rec.model_dump_json()
                key_sets[_keys(line)] += 1
                lines.append(line)
                stats["rows"] += 1
                stats["in_graph_scope"] += rec.category_major is not None
                stats["image_ref"] += node.image_ref is not None
                stats["image_url"] += node.image_url is not None
                stats["snap_id"] += node.snap_id is not None
                stats["has_image"] += node.has_image
        if not stats["rows"]:
            raise CombineError(f"{path}: 레코드가 없다")
        by_source[source] = dict(stats)
    if len(key_sets) != 1:
        raise CombineError(f"키(필드명) 집합이 {len(key_sets)}가지다: 모든 줄이 같아야 한다")
    top_keys, node_keys = next(iter(key_sets))
    report = {
        "rows": len(lines),
        "by_source": by_source,
        "missing_image_ref": sum(s["rows"] - s["image_ref"] for s in by_source.values()),
        "fields": {"record": list(top_keys), "node": list(node_keys)},
    }
    return lines, report
