"""이미지 인덱스(parquet, 계약 v1.1)를 Product에 붙인다. 인덱스는 선택이고, 없으면 이미지 상태를 채우지 않는다.

- 원천은 C2 하나다. T2는 `image_ref`·`image_scope`·`image_status`를 **그대로 승계**하고 다시 계산하지 않는다(결정 G0-C2).
- 조인 키는 `product_id`. 02 세트 구성품(`<원 id>#<k>`)은 `parent_id`로 원 행을 찾아 승계하고 scope만 `full_set`.
- `multi_wearer_suspected`는 인덱스가 True일 때만 True, 그 밖은 None(조건 미발동일 뿐 "한 명"의 확정이 아니다).
- `local_path`는 개인 절대 경로라 Product에 싣지 않는다(L0 #124). 표현층은 인덱스에서 직접 읽는다.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Optional

from trend_agent.schemas.graph_schema import ProductRecord

INDEX_COLUMNS = ["product_id", "source", "row", "image_ref", "image_scope", "image_status", "multi_wearer_suspected"]


def _nn(v: Any) -> Any:
    """pandas NaN/None → None."""
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
    except (TypeError, ValueError):
        pass
    return v


def load_image_index(path: Path | str) -> dict[str, dict[str, Any]]:
    import pandas as pd

    df = pd.read_parquet(path, columns=INDEX_COLUMNS)
    out: dict[str, dict[str, Any]] = {}
    for rec in df.to_dict(orient="records"):
        out[rec["product_id"]] = {k: _nn(v) for k, v in rec.items()}
    return out


class ImageJoiner:
    """레코드마다 apply(). 결과 건수는 stats에 모은다(리포트용)."""

    def __init__(self, index: Optional[dict[str, dict[str, Any]]]):
        self.index = index
        self.stats: Counter = Counter()

    def apply(self, rec: ProductRecord) -> ProductRecord:
        node = rec.node
        if self.index is None:
            self.stats["no_index"] += 1
            return rec
        key = node.parent_id or node.id
        row = self.index.get(key)
        if row is None:
            self.stats["missing_in_index"] += 1
            return rec
        node.image_ref = row.get("image_ref")
        node.image_scope = "full_set" if node.parent_id else row.get("image_scope")
        node.image_status = row.get("image_status")
        node.multi_wearer_suspected = True if row.get("multi_wearer_suspected") is True else None
        if node.source == "musinsa_snap" and node.snap_id is not None and _nn(row.get("row")) is not None \
                and int(row["row"]) != node.snap_id:
            self.stats["snap_row_mismatch"] += 1
        if node.source == "otpensource" and (node.image_url or None) != (row.get("image_ref") or None):
            self.stats["otp_url_mismatch"] += 1
        self.stats["joined_parent" if node.parent_id else "joined"] += 1
        return rec
