"""image_ref 조회 함수. 합성 parquet로 확인한다(실제 데이터셋 불필요)."""
import pytest

from fashion_planner_trend.images import (
    SNAP_REPO_ID,
    SNAP_REVISION,
    ImageRefError,
    ImageResolver,
    parse_snap_ref,
    sniff_media_type,
)
from fashion_planner_trend.ingestion.normalization import musinsa_snap

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 10


def _ref(row: int, rev: str = SNAP_REVISION) -> str:
    return f"hf://datasets/{SNAP_REPO_ID}@{rev}#row={row}"


@pytest.fixture
def parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    images = [{"bytes": JPEG + bytes([i]), "path": None} for i in range(25)]
    table = pa.table({"image": images, "label": [str(i) for i in range(25)]})
    path = tmp_path / "snap.parquet"
    pq.write_table(table, path, row_group_size=10)  # 여러 row group에 걸친 조회를 확인
    return path


def test_same_revision_as_normalizer():
    assert (SNAP_REPO_ID, SNAP_REVISION) == (musinsa_snap.REPO_ID, musinsa_snap.REVISION)
    assert musinsa_snap.image_ref(7) == _ref(7)


def test_parse_snap_ref():
    assert parse_snap_ref(_ref(9665)) == 9665
    with pytest.raises(ImageRefError):
        parse_snap_ref(_ref(1, rev="deadbeef"))  # 다른 리비전의 행은 다른 사진일 수 있다
    with pytest.raises(ImageRefError):
        parse_snap_ref("hf://datasets/other/repo@abc#row=1")


def test_snap_ref_reads_parquet_row_across_groups(parquet):
    r = ImageResolver(snap_parquet=parquet)
    for row in (0, 9, 10, 24, 3):
        img = r.resolve(_ref(row))
        assert img.kind == "bytes" and img.origin == "parquet" and img.media_type == "image/jpeg"
        assert img.data == JPEG + bytes([row])
    with pytest.raises(ImageRefError):
        r.resolve(_ref(25))  # 범위 밖


def test_snap_dir_file_wins_over_parquet(parquet, tmp_path):
    d = tmp_path / "snap"
    d.mkdir()
    (d / "4.jpg").write_bytes(JPEG + b"local")
    r = ImageResolver(snap_parquet=parquet, snap_dir=d)
    assert r.resolve(_ref(4)).origin == "local_file" and r.resolve(_ref(4)).data.endswith(b"local")
    assert r.resolve(_ref(5)).origin == "parquet"  # 파일이 없으면 parquet


def test_url_ref_is_returned_without_fetching():
    img = ImageResolver(snap_parquet="does-not-exist.parquet").resolve("https://image.example.invalid/a.jpg")
    assert img.kind == "url" and img.url == "https://image.example.invalid/a.jpg" and img.data is None


def test_bad_refs():
    r = ImageResolver()
    for ref in (None, "", "s3://bucket/x.jpg"):
        with pytest.raises(ImageRefError):
            r.resolve(ref)


def test_sniff_media_type():
    assert sniff_media_type(JPEG) == "image/jpeg"
    assert sniff_media_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff_media_type(b"hello") == "application/octet-stream"
