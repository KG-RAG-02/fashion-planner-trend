"""v2 빌드 산출물 안전장치·이미지 조인 (합성 입력)."""
import os

import pytest

from trend_agent.ingestion.normalization.image_index import ImageJoiner
from trend_agent.ingestion.normalization.otpensource import OtpenNormalizer
from trend_agent.ingestion.normalization.musinsa_snap import SnapNormalizer
from trend_agent.ingestion.normalization.output import UnsafeOutput, resolve_out, write_lines_atomic
from trend_agent.ingestion.normalization.set_split import SetSplitter, apply_split


def _symlink_supported(tmp_path) -> bool:
    """Windows는 관리자 권한이나 개발자 모드가 없으면 symlink를 만들 수 없다 (그 환경에서는 symlink 테스트를 건너뛴다)."""
    try:
        (tmp_path / "_probe_target").mkdir()
        (tmp_path / "_probe_link").symlink_to(tmp_path / "_probe_target")
    except OSError:
        return False
    return True


def test_resolve_out_refuses_v1_and_symlinks(tmp_path):
    if not _symlink_supported(tmp_path):
        pytest.skip("symlink를 만들 수 없는 환경(Windows 권한)")
    v1 = tmp_path / "data" / "processed" / "v1"
    v1.mkdir(parents=True)
    with pytest.raises(UnsafeOutput):
        resolve_out(v1, protected=[v1])
    with pytest.raises(UnsafeOutput):
        resolve_out(v1 / "sub", protected=[v1])
    link = tmp_path / "link"
    link.symlink_to(v1)
    with pytest.raises(UnsafeOutput):
        resolve_out(link, protected=[v1])
    ok = resolve_out(tmp_path / "v2", protected=[v1])
    assert ok.is_dir()


def test_write_refuses_symlinked_target(tmp_path):
    if not _symlink_supported(tmp_path):
        pytest.skip("symlink를 만들 수 없는 환경(Windows 권한)")
    real = tmp_path / "v1.jsonl"
    real.write_text("orig\n")
    (tmp_path / "out").mkdir()
    target = tmp_path / "out" / "x.jsonl"
    target.symlink_to(real)
    with pytest.raises(UnsafeOutput):
        write_lines_atomic(target, ["new"])
    assert real.read_text() == "orig\n"  # 링크를 통해 원본을 덮어쓰지 않는다
    write_lines_atomic(tmp_path / "out" / "y.jsonl", ["a", "b"])
    assert (tmp_path / "out" / "y.jsonl").read_text() == "a\nb\n"


def test_image_join_inherits_status_and_parent_for_components():
    idx = {
        "otpensource:f.json:7": {"product_id": "otpensource:f.json:7", "source": "otpensource", "row": 7,
                                 "image_ref": "http://x/set.jpg", "image_scope": "catalog", "image_status": "broken",
                                 "multi_wearer_suspected": None},
        "musinsa_snap:3:상의": {"product_id": "musinsa_snap:3:상의", "source": "musinsa_snap", "row": 3,
                               "image_ref": "hf://datasets/disl/musinsa-snap-new@r#row=3", "image_scope": "full_snap",
                               "image_status": "ok", "multi_wearer_suspected": True},
        "musinsa_snap:3:하의": {"product_id": "musinsa_snap:3:하의", "source": "musinsa_snap", "row": 3,
                               "image_ref": "hf://datasets/disl/musinsa-snap-new@r#row=3", "image_scope": "full_snap",
                               "image_status": "ok", "multi_wearer_suspected": None},
    }
    j = ImageJoiner(idx)
    norm = OtpenNormalizer()
    row = {"_file": "f.json", "_index": 7, "sub_category": "슈트/블레이저 재킷", "big_category": "아우터",
           "product_name": "[SET] Sample jacket & Sample Pleats Skirt", "image_url": "http://x/set.jpg"}
    comps = apply_split(norm.normalize_row(row), SetSplitter().decide(row["product_name"]), norm, row)
    for c in comps:
        j.apply(c)
        assert (c.node.image_ref, c.node.image_scope, c.node.image_status) == ("http://x/set.jpg", "full_set", "broken")
        assert c.node.has_image is False
    sn = SnapNormalizer()
    top = j.apply(sn.normalize_slot({"file_no": "1", "slot": "상의", "attrs": {"카테고리": "탑"}, "k": 0, "row": 3}))
    bottom = j.apply(sn.normalize_slot({"file_no": "1", "slot": "하의", "attrs": {"카테고리": "팬츠"}, "k": 0, "row": 3}))
    assert top.node.image_ref == bottom.node.image_ref and top.node.image_scope == "full_snap"  # 같은 사진 1장(Q4)
    assert top.node.multi_wearer_suspected is True and bottom.node.multi_wearer_suspected is None  # False는 올리지 않는다
    assert j.stats["joined_parent"] == 2 and j.stats["joined"] == 2 and not j.stats["snap_row_mismatch"]


def test_image_join_without_index_leaves_fields_empty():
    j = ImageJoiner(None)
    r = j.apply(SnapNormalizer().normalize_slot({"file_no": "1", "slot": "하의", "attrs": {"카테고리": "팬츠"}, "k": 0, "row": 0}))
    assert r.node.image_status is None and r.node.image_ref is None and j.stats["no_index"] == 1
