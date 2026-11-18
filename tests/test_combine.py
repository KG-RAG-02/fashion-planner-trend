"""02·03 통합본(musinsa_dataset) 합치기 (합성 입력)."""
import json

import pytest

from fashion_planner_trend.ingestion.normalization.combine import CombineError, combine_products
from fashion_planner_trend.ingestion.normalization.musinsa_snap import SnapNormalizer
from fashion_planner_trend.ingestion.normalization.otpensource import OtpenNormalizer


def _otp(i):
    return OtpenNormalizer().normalize_row({"_file": "f.json", "_index": i, "sub_category": "셔츠", "big_category": "상의",
                                            "product_name": f"셔츠 {i}", "image_url": f"http://x/{i}.jpg"})


def _snap(row, slot="상의"):
    return SnapNormalizer().normalize_slot({"file_no": "1", "slot": slot, "attrs": {"카테고리": "탑"}, "k": 0, "row": row})


def _write(path, recs):
    path.write_text("".join(r.model_dump_json() + "\n" for r in recs), encoding="utf-8")
    return path


def test_combine_same_keys_and_source_kept(tmp_path):
    a = _write(tmp_path / "otp.jsonl", [_otp(0), _otp(1)])
    b = _write(tmp_path / "snap.jsonl", [_snap(0), _snap(0, "하의"), _snap(1)])
    lines, report = combine_products([("otpensource", a), ("musinsa_snap", b)])
    rows = [json.loads(x) for x in lines]
    assert len(rows) == 5 == report["rows"]
    assert len({(tuple(sorted(r)), tuple(sorted(r["node"]))) for r in rows}) == 1  # 모든 줄의 필드명이 같다
    assert [r["node"]["source"] for r in rows] == ["otpensource"] * 2 + ["musinsa_snap"] * 3
    assert all(r["node"]["image_ref"] for r in rows) and report["missing_image_ref"] == 0
    assert rows[0]["node"]["snap_id"] is None and rows[2]["node"]["image_url"] is None  # 출처에 없는 값은 null
    assert report["by_source"]["musinsa_snap"]["snap_id"] == 3


def test_combine_rejects_wrong_source_and_duplicate_ids(tmp_path):
    a = _write(tmp_path / "otp.jsonl", [_otp(0)])
    with pytest.raises(CombineError, match="source"):
        combine_products([("musinsa_snap", a)])
    with pytest.raises(CombineError, match="중복"):
        combine_products([("otpensource", a), ("otpensource", a)])


def test_combine_rejects_empty_input(tmp_path):
    empty = tmp_path / "e.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(CombineError):
        combine_products([("otpensource", empty)])
