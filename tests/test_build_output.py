"""v2 빌드 산출물 안전장치·이미지 조인 (합성 입력)."""
import os

import pytest

from trend_agent.ingestion.normalization.image_index import ImageJoiner
from trend_agent.ingestion.normalization.otpensource import OtpenNormalizer, drop_without_image
from trend_agent.ingestion.normalization.musinsa_snap import IMAGE_BASE, REPO_ID as SNAP_REPO, REVISION as SNAP_REV, SnapNormalizer
from trend_agent.ingestion.normalization.output import REPO_ROOT, UnsafeOutput, protected_dirs, resolve_out, write_lines_atomic
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
    # 인덱스의 리비전("@r")이 정규화가 만든 참조와 다르다 → 세고 인덱스 값을 쓴다 (D-33)
    assert j.stats["image_ref_mismatch"] == 2 and top.node.image_ref.endswith("@r#row=3")


def test_image_join_without_index_keeps_normalized_ref_and_no_status():
    j = ImageJoiner(None)
    r = j.apply(SnapNormalizer().normalize_slot({"file_no": "1", "slot": "하의", "attrs": {"카테고리": "팬츠"}, "k": 0, "row": 0}))
    assert r.node.image_status is None and j.stats["no_index"] == 1  # 상태는 인덱스에서만 온다
    assert r.node.image_ref == f"{IMAGE_BASE}0" and r.node.image_scope == "full_snap" and r.node.has_image


def test_image_join_same_ref_is_not_mismatch():
    sn = SnapNormalizer()
    rec = sn.normalize_slot({"file_no": "1", "slot": "상의", "attrs": {"카테고리": "탑"}, "k": 0, "row": 5})
    idx = {rec.node.id: {"product_id": rec.node.id, "source": "musinsa_snap", "row": 5, "image_ref": f"{IMAGE_BASE}5",
                         "image_scope": "full_snap", "image_status": "ok", "multi_wearer_suspected": None}}
    j = ImageJoiner(idx)
    j.apply(rec)
    assert not j.stats["image_ref_mismatch"] and rec.node.image_status == "ok"


def test_snap_image_ref_points_to_parquet_row():
    """03: snap_id = parquet 행, image_ref = 고정 리비전 parquet의 그 행. 같은 사진의 슬롯은 같은 참조."""
    sn = SnapNormalizer()
    a = sn.normalize_slot({"file_no": "9", "slot": "상의", "attrs": {"카테고리": "탑"}, "k": 0, "row": 42})
    b = sn.normalize_slot({"file_no": "9", "slot": "하의", "attrs": {"카테고리": "팬츠"}, "k": 0, "row": 42})
    assert a.node.snap_id == b.node.snap_id == 42
    assert a.node.image_ref == b.node.image_ref == f"hf://datasets/{SNAP_REPO}@{SNAP_REV}#row=42"


def test_otp_image_ref_equals_url_and_components_inherit():
    norm = OtpenNormalizer()
    row = {"_file": "f.json", "_index": 7, "sub_category": "슈트/블레이저 재킷", "big_category": "아우터",
           "product_name": "[SET] Sample jacket & Sample Pleats Skirt", "image_url": "http://x/set.jpg"}
    parent = norm.normalize_row(row)
    assert (parent.node.image_ref, parent.node.image_scope) == ("http://x/set.jpg", "catalog")
    comps = apply_split(parent, SetSplitter().decide(row["product_name"]), norm, row)
    assert comps and all((c.node.image_ref, c.node.image_scope) == ("http://x/set.jpg", "full_set") for c in comps)
    empty = norm.normalize_row({"_file": "f.json", "_index": 8, "sub_category": "", "product_name": "", "image_url": ""})
    assert empty.node.image_ref is None and empty.node.image_scope is None


def test_drop_without_image_removes_only_url_less_rows():
    norm = OtpenNormalizer()
    rows = [{"_file": "f.json", "_index": 0, "sub_category": "셔츠", "big_category": "상의", "product_name": "셔츠", "image_url": "http://x/0.jpg"},
            {"_file": "f.json", "_index": 1, "sub_category": "", "big_category": "상의", "product_name": "", "image_url": ""},
            {"_file": "f.json", "_index": 2, "sub_category": "", "big_category": "", "product_name": "", "image_url": " "}]
    kept, stats = drop_without_image([norm.normalize_row(r) for r in rows])
    assert [r.node.id for r in kept] == ["otpensource:f.json:0"]
    assert stats == {"dropped_no_image_url": 2, "dropped_in_graph_scope": 0, "dangling_dup_of": 0, "dangling_parent_id": 0}


def test_repo_v1_folder_is_always_protected():
    assert (REPO_ROOT / "data" / "processed" / "v1").resolve() in [p.resolve() for p in protected_dirs()]
    with pytest.raises(UnsafeOutput):
        resolve_out(REPO_ROOT / "data" / "processed" / "v1")
