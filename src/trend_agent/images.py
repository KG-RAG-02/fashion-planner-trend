"""`image_ref` → 실제 이미지 (D-33의 조회 함수, 간단한 로컬 버전).

그래프·JSONL에는 주소(`image_ref`)만 두고, 이미지는 보여 줄 때 이 모듈로 꺼낸다.
- 03 `hf://datasets/disl/musinsa-snap-new@<rev>#row=<n>` → JPEG **바이트**. 찾는 순서:
  ① `snap_dir/<n>.jpg` (fetch_snap_images.py extract 결과, 있으면) ② 고정 리비전 parquet의 n번째 행 `image.bytes`.
  parquet은 100행 단위 row group이라 행 하나를 읽을 때 그 group만 읽는다(마지막 group은 메모리에 둔다).
- 02 `http(s)://…` → **URL만** 돌려준다. 이 모듈은 외부로 요청을 보내지 않는다(내려받기·핫링크 여부는 표현층이 정한다).
- 운영에서는 같은 인터페이스(`resolve(ref) -> ResolvedImage`)로 구현만 바꾼다(예: 오브젝트 스토리지 서명 URL).
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

SNAP_REPO_ID = "disl/musinsa-snap-new"
SNAP_REVISION = "4335d233ef3b54bc5132a4d572d589042a41219d"  # musinsa_snap.REVISION과 같아야 한다(테스트가 확인)
SNAP_FILENAME = "data/train-00000-of-00001.parquet"
_HF_REF = re.compile(r"hf://datasets/(?P<repo>[^@#]+)@(?P<rev>[0-9a-f]+)#row=(?P<row>\d+)")


class ImageRefError(ValueError):
    """형식이 틀렸거나 지원하지 않는 참조, 또는 범위 밖 행."""


class ImageUnavailable(LookupError):
    """참조는 맞지만 이 환경에 원본이 없다(parquet 미다운로드 등)."""


@dataclass(frozen=True)
class ResolvedImage:
    ref: str
    kind: Literal["bytes", "url"]
    data: Optional[bytes] = None        # kind == "bytes"
    url: Optional[str] = None           # kind == "url"
    media_type: Optional[str] = None    # bytes일 때 매직 바이트로 판정
    origin: Literal["local_file", "parquet", "remote_url"] = "parquet"


def parse_snap_ref(ref: str) -> int:
    """03 참조에서 parquet 행 번호. 다른 데이터셋·리비전이면 ImageRefError (다른 리비전의 행은 다른 사진일 수 있다)."""
    m = _HF_REF.fullmatch(ref)
    if not m:
        raise ImageRefError(f"hf 참조 형식이 아니다: {ref!r}")
    if m["repo"] != SNAP_REPO_ID or m["rev"] != SNAP_REVISION:
        raise ImageRefError(f"지원하지 않는 데이터셋/리비전: {m['repo']}@{m['rev']}")
    return int(m["row"])


def sniff_media_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def find_snap_parquet(allow_download: bool = False) -> Path:
    """HF 캐시의 고정 리비전 parquet 경로. 캐시에 없으면 allow_download일 때만 내려받는다."""
    from huggingface_hub import hf_hub_download

    try:
        return Path(hf_hub_download(SNAP_REPO_ID, SNAP_FILENAME, repo_type="dataset", revision=SNAP_REVISION,
                                    local_files_only=not allow_download))
    except Exception as exc:  # 캐시 없음·오프라인
        raise ImageUnavailable(f"03 parquet이 없다 (allow_download=True로 받거나 HF 캐시를 준비): {exc}") from exc


class ImageResolver:
    """`resolve(image_ref)`. parquet은 처음 03 참조를 풀 때 연다."""

    def __init__(self, snap_parquet: Path | str | None = None, snap_dir: Path | str | None = None,
                 allow_download: bool = False):
        self._parquet_path = Path(snap_parquet) if snap_parquet else None
        self.snap_dir = Path(snap_dir) if snap_dir else None
        self.allow_download = allow_download
        self._pf = None
        self._starts: list[int] = []   # row group별 첫 행 번호
        self._cached: tuple[int, list] | None = None  # (row group 번호, image 열 값)

    def resolve(self, ref: Optional[str]) -> ResolvedImage:
        if not ref:
            raise ImageRefError("image_ref가 비었다")
        if ref.startswith(("http://", "https://")):
            return ResolvedImage(ref=ref, kind="url", url=ref, origin="remote_url")
        if ref.startswith("hf://"):
            return self._snap(ref, parse_snap_ref(ref))
        raise ImageRefError(f"지원하지 않는 참조: {ref!r}")

    # ---------- 03 ----------
    def _snap(self, ref: str, row: int) -> ResolvedImage:
        if self.snap_dir is not None:
            path = self.snap_dir / f"{row}.jpg"
            if path.is_file():
                data = path.read_bytes()
                return ResolvedImage(ref=ref, kind="bytes", data=data, media_type=sniff_media_type(data), origin="local_file")
        data = self._parquet_bytes(row)
        return ResolvedImage(ref=ref, kind="bytes", data=data, media_type=sniff_media_type(data), origin="parquet")

    def _open(self):
        if self._pf is None:
            import pyarrow.parquet as pq

            path = self._parquet_path or find_snap_parquet(self.allow_download)
            if not Path(path).is_file():
                raise ImageUnavailable(f"03 parquet이 없다: {path}")
            self._pf = pq.ParquetFile(path)
            meta, start = self._pf.metadata, 0
            for i in range(meta.num_row_groups):
                self._starts.append(start)
                start += meta.row_group(i).num_rows
        return self._pf

    def _parquet_bytes(self, row: int) -> bytes:
        pf = self._open()
        if not 0 <= row < pf.metadata.num_rows:
            raise ImageRefError(f"행 번호 범위 밖: {row} (0..{pf.metadata.num_rows - 1})")
        group = bisect_right(self._starts, row) - 1
        if self._cached is None or self._cached[0] != group:
            self._cached = (group, pf.read_row_group(group, columns=["image"]).column("image").to_pylist())
        cell = self._cached[1][row - self._starts[group]]
        data = (cell or {}).get("bytes")
        if not data:
            raise ImageUnavailable(f"행 {row}에 이미지 바이트가 없다")
        return data


_default: Optional[ImageResolver] = None


def resolve_image(ref: Optional[str]) -> ResolvedImage:
    """기본 설정(HF 캐시 parquet, 다운로드 안 함)으로 푼다. 설정을 바꾸려면 ImageResolver를 직접 만든다."""
    global _default
    if _default is None:
        _default = ImageResolver()
    return _default.resolve(ref)
