"""Product id 또는 image_ref로 이미지를 꺼낸다 (조회 함수 확인용).

실행:  python scripts/resolve_image.py musinsa_snap:9665:하의            # 통합본에서 id로 image_ref를 찾아 푼다
       python scripts/resolve_image.py "hf://datasets/disl/musinsa-snap-new@<rev>#row=9665"
       python scripts/resolve_image.py <id 또는 ref> ... [--out DIR] [--snap-dir DIR] [--dataset PATH]
출력:  03(바이트) → <out>/<id 또는 row>.jpg 로 저장하고 경로를 출력 (기본 out = data/processed/images_preview)
       02(URL)   → URL만 출력 (내려받지 않는다)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fashion_planner_trend.images import ImageResolver, ImageRefError, ImageUnavailable  # noqa: E402

DATASET = ROOT / "data" / "processed" / "musinsa_dataset.jsonl"
OUT = ROOT / "data" / "processed" / "images_preview"
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def lookup_refs(ids: set[str], dataset: Path) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    with dataset.open(encoding="utf-8") as f:
        for line in f:
            node = json.loads(line)["node"]
            if node["id"] in ids:
                found[node["id"]] = node.get("image_ref")
                if len(found) == len(ids):
                    break
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="Product id 또는 image_ref")
    ap.add_argument("--out", default=str(OUT), help="바이트 이미지를 저장할 폴더")
    ap.add_argument("--snap-dir", help="추출된 03 jpg 폴더(<row>.jpg). 있으면 parquet보다 먼저 쓴다")
    ap.add_argument("--dataset", default=str(DATASET), help="id → image_ref를 찾을 통합본")
    a = ap.parse_args()

    ids = {t for t in a.targets if not re.match(r"^(hf|https?)://", t)}
    refs = lookup_refs(ids, Path(a.dataset)) if ids else {}
    resolver = ImageResolver(snap_dir=a.snap_dir)
    out = Path(a.out)
    failed = 0
    for t in a.targets:
        ref = refs.get(t) if t in ids else t
        if t in ids and t not in refs:
            print(f"{t}\t없음: 통합본에 이 id가 없다")
            failed += 1
            continue
        try:
            img = resolver.resolve(ref)
        except (ImageRefError, ImageUnavailable) as exc:
            print(f"{t}\t실패: {exc}")
            failed += 1
            continue
        if img.kind == "url":
            print(f"{t}\tURL\t{img.url}")
            continue
        out.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w.-]+", "_", t if t in ids else ref.rsplit("=", 1)[-1])
        path = out / f"{stem}{EXT.get(img.media_type, '.bin')}"
        path.write_bytes(img.data)
        print(f"{t}\t{img.media_type}\t{len(img.data):,} bytes\t{img.origin}\t{path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
