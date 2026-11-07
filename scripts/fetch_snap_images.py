"""Internal C2 image pipeline. Never crop, re-encode, resize, upload, or commit data.

Run with the shared venv after G0, using --shared "$SHARED". Phases:
download -> extract -> (check_image_urls.py) -> index -> contact.
The final index is published only after all source and integrity checks pass.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import io
import json
import logging
import os
from pathlib import Path
import random
import subprocess
import sys

REPO_ID = "disl/musinsa-snap-new"
REVISION = "4335d233ef3b54bc5132a4d572d589042a41219d"
FILENAME = "data/train-00000-of-00001.parquet"
IMAGE_BASE = f"hf://datasets/{REPO_ID}@{REVISION}#row="
LOG = logging.getLogger("C2.images")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@contextmanager
def exclusive(path: Path):
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def validate_download(a) -> dict:
    """Trust only the pinned HF LFS ETag, or an explicitly supplied SHA256."""
    receipt_path = a.images / "download_manifest.json"
    old = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    digest = sha256(a.parquet) if a.parquet.exists() else None
    valid_old = (
        old.get("revision") == REVISION and old.get("repo_id") == REPO_ID
        and old.get("filename") == FILENAME and old.get("status") == "verified"
        and old.get("sha256") == digest and old.get("bytes") == a.parquet.stat().st_size
    ) if digest else False
    if valid_old and (not a.expected_sha256 or digest == a.expected_sha256.lower()):
        LOG.info("Revalidated pinned Parquet SHA256 %s", digest)
        return old
    if a.expected_sha256:
        if not a.parquet.exists() or digest != a.expected_sha256.lower():
            raise ValueError("Existing Parquet does not match --expected-sha256")
        metadata_source, expected, expected_size = "operator_supplied_sha256", digest, a.parquet.stat().st_size
    else:
        if a.offline:
            raise ValueError("Offline Parquet needs a verified receipt or --expected-sha256")
        # HF_HOME must be set before importing huggingface_hub.
        from huggingface_hub import get_hf_file_metadata, hf_hub_download, hf_hub_url
        meta = get_hf_file_metadata(
            hf_hub_url(REPO_ID, FILENAME, repo_type="dataset", revision=REVISION), timeout=10
        )
        if meta.commit_hash != REVISION:
            raise ValueError("HF resolved a different revision")
        expected = (meta.etag or "").strip('"')
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ValueError("HF metadata does not provide a SHA256 LFS ETag")
        expected_size, metadata_source = meta.size, "pinned_hf_lfs_etag"
        downloaded = Path(hf_hub_download(
            repo_id=REPO_ID, filename=FILENAME, repo_type="dataset", revision=REVISION,
            local_dir=a.shared / "hf", library_name="VOGUE-Research-ImageAudit", library_version="1.0",
        ))
        if downloaded.resolve() != a.parquet.resolve():
            raise ValueError("Downloaded file differs from the selected Parquet path")
        digest = sha256(a.parquet)
    if digest != expected or a.parquet.stat().st_size != expected_size:
        raise ValueError("Parquet checksum/size validation failed")
    receipt = dict(repo_id=REPO_ID, revision=REVISION, filename=FILENAME,
                   local_path=str(a.parquet), sha256=digest, bytes=expected_size,
                   verification=metadata_source, status="verified", checked_at=utc())
    atomic_json(receipt_path, receipt)
    a.parquet.with_suffix(".parquet.sha256").write_text(f"{digest}  {a.parquet.name}\n")
    LOG.info("Pinned Parquet verified: bytes=%s sha256=%s", expected_size, digest)
    return receipt


def normalizer(a):
    sys.path.insert(0, str(a.repo / "src"))
    from trend_agent.ingestion.normalization import musinsa_snap
    return musinsa_snap


def dict_items(value):
    # Matches iter_slots exactly, including original tuple order and k gaps.
    return [(k, item) for k, item in enumerate(list(value) if isinstance(value, tuple) else [value])
            if isinstance(item, dict)]


def suspicion(parsed: dict) -> dict:
    labels, rects, repeated, invalid_rects = {}, {}, [], []
    for key, value in parsed.items():
        slot = key.rsplit(".", 1)[-1]
        if ".라벨링." in key:
            items = dict_items(value)
            labels[slot] = len(items)
            if any(k >= 1 for k, _ in items):
                repeated.append(slot)
        elif ".렉트좌표." in key:
            items = dict_items(value)
            rects[slot] = sum(bool(d) and "X좌표" in d and "Y좌표" in d for _, d in items)
            invalid_rects.extend(slot for _, d in items if not d or "X좌표" not in d or "Y좌표" not in d)
    label_count, rect_count = sum(labels.values()), sum(rects.values())
    reasons = (["tuple_slot_k_ge_1"] if repeated else [])
    if rect_count < label_count:
        reasons.append("rect_count_lt_label_count")
    return dict(multi_wearer_suspected=bool(reasons), reasons=reasons,
                label_count=label_count, rect_count=rect_count, label_counts_by_slot=labels,
                rect_counts_by_slot=rects, tuple_slots=repeated, invalid_rect_slots=invalid_rects)


def extract(a) -> None:
    import pyarrow.parquet as pq
    from PIL import Image
    validate_download(a)
    module = normalizer(a)
    pf = pq.ParquetFile(a.parquet)
    if pf.metadata.num_rows != a.expected_rows:
        raise ValueError(f"Parquet rows={pf.metadata.num_rows}, expected={a.expected_rows}")
    snap = a.images / "snap"
    snap.mkdir(exist_ok=True)
    tmp = a.images / "snap_manifest.jsonl.partial"
    failures, row = [], 0
    with tmp.open("w", encoding="utf-8") as manifest:
        for batch in pf.iter_batches(batch_size=64, columns=["image", "label"]):
            for record in batch.to_pylist():
                image = record["image"]
                data = image.get("bytes") if isinstance(image, dict) else None
                if not isinstance(data, bytes):
                    raise ValueError(f"Missing raw image bytes at row {row}")
                path = snap / f"{row}.jpg"
                digest = hashlib.sha256(data).hexdigest()
                if path.exists():
                    if sha256(path) != digest:
                        raise ValueError(f"Existing image bytes differ at row {row}; refusing overwrite")
                else:
                    part = path.with_suffix(".jpg.partial")
                    part.write_bytes(data)  # Exact bytes; never call Image.save/resize/crop.
                    part.replace(path)
                width = height = None
                status, fmt = "ok", None
                try:
                    with Image.open(io.BytesIO(data)) as im:
                        width, height, fmt = *im.size, im.format
                        im.load()  # Decode only to check integrity; no image is generated.
                    if fmt != "JPEG":
                        status = "unexpected_format"
                except Exception as exc:
                    status = f"decode_error:{type(exc).__name__}"
                if status != "ok":
                    failures.append(dict(row=row, image_status=status))
                entry = dict(row=row, bytes=len(data), sha256=digest, width=width, height=height,
                             format=fmt, image_status="ok" if status == "ok" else "broken", decode_status=status,
                             **suspicion(module.parse_label(record["label"])))
                manifest.write(json.dumps(entry, ensure_ascii=False) + "\n")
                row += 1
                if row % 500 == 0:
                    LOG.info("Verified original photo bytes: %s/%s", row, a.expected_rows)
    tmp.replace(a.images / "snap_manifest.jsonl")
    rows = load_jsonl(a.images / "snap_manifest.jsonl")
    summary = dict(rows=row, bytes=sum(r["bytes"] for r in rows), failures=failures,
                   resolutions=dict(Counter(f'{r["width"]}x{r["height"]}' for r in rows)),
                   suspected_snap_rows=sum(r["multi_wearer_suspected"] for r in rows),
                   reason_counts=dict(Counter(reason for r in rows for reason in r["reasons"])),
                   parquet_sha256=sha256(a.parquet), checked_at=utc())
    atomic_json(a.images / "extraction_summary.json", summary)
    if row != a.expected_rows:
        raise ValueError("Image extraction row count differs from expected input")
    if failures:
        LOG.warning("Preserved %s original photos with decode/format failures; status=broken", len(failures))
    LOG.info("Original photo extraction complete: %s rows, %s bytes", row, summary["bytes"])


def verify_ids(a, slots, module, processed) -> dict:
    norm = module.SnapNormalizer()
    generated = [norm.normalize_slot(s).node for s in slots]
    expected_ids = [n.id for n in generated]
    actual_ids = [r["node"]["id"] for r in processed]
    expected, actual = Counter(expected_ids), Counter(actual_ids)
    by_id = {r["node"]["id"]: r["node"] for r in processed}
    report = dict(
        expected_rows=a.expected_products, generated_rows=len(slots), processed_rows=len(processed),
        generated_duplicate_ids=sum(v - 1 for v in expected.values()),
        processed_duplicate_ids=sum(v - 1 for v in actual.values()),
        missing_count=sum((expected - actual).values()), extra_count=sum((actual - expected).values()),
        missing_examples=list((expected - actual))[:20], extra_examples=list((actual - expected))[:20],
        order_mismatch_count=sum(x != y for x, y in zip(expected_ids, actual_ids)) + abs(len(expected_ids) - len(actual_ids)),
        source_row_mismatch_count=sum(n.source_row != by_id[n.id].get("source_row") for n in generated if n.id in by_id),
        source_mismatch_count=sum(r["node"].get("source") != "musinsa_snap" for r in processed),
        normalizer_sha256=sha256(Path(module.__file__)), input_sha256=sha256(a.snap_jsonl),
        parquet_sha256=sha256(a.parquet), checked_at=utc(),
    )
    numeric_checks = [report[k] for k in (
        "generated_duplicate_ids", "processed_duplicate_ids", "missing_count", "extra_count",
        "order_mismatch_count", "source_row_mismatch_count", "source_mismatch_count")]
    report["passed"] = not any(numeric_checks) and len(slots) == len(processed) == a.expected_products
    atomic_json(a.images / "id_integrity.json", report)
    if not report["passed"]:
        raise ValueError("Source-to-v1 ID alignment failed; see id_integrity.json")
    return report


def build_index(a) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    validate_download(a)
    module = normalizer(a)
    selected = sorted(a.parquet.parent.rglob("*.parquet"))
    if not selected or selected[0].resolve() != a.parquet.resolve():
        raise ValueError("Repository iter_slots would select a different Parquet")
    slots = list(module.iter_slots(a.parquet.parent))
    snap_records, catalog = load_jsonl(a.snap_jsonl), load_jsonl(a.catalog_jsonl)
    report = verify_ids(a, slots, module, snap_records)
    photos = load_jsonl(a.images / "snap_manifest.jsonl")
    if len(photos) != a.expected_rows or {p["row"] for p in photos} != set(range(a.expected_rows)):
        raise ValueError("Photo manifest rows are incomplete or duplicated")
    by_row = {p["row"]: p for p in photos}
    # Independent second pass checks stored files, not just extraction's in-memory bytes.
    for p in photos:
        path = a.images / "snap" / f'{p["row"]}.jpg'
        if p["image_status"] not in {"ok", "broken"} or path.stat().st_size != p["bytes"] or sha256(path) != p["sha256"]:
            raise ValueError(f'Stored original photo integrity failed: row {p["row"]}')
    if len(catalog) != a.expected_catalog:
        raise ValueError("Unexpected catalog row count")
    url_meta = json.loads((a.images / "url_checks_meta.json").read_text())
    if url_meta["input_sha256"] != sha256(a.catalog_jsonl):
        raise ValueError("URL audit input differs from catalog input")
    if url_meta.get("policy", {}).get("method") != "GET":
        raise ValueError("G0-C2 requires GET-only URL receipts; legacy HEAD receipts cannot be used")
    checks = load_jsonl(a.url_results)
    if len({r["product_id"] for r in checks}) != len(checks):
        raise ValueError("Duplicate URL audit product IDs")
    checked = {r["product_id"]: r for r in checks}
    expected_checked = {r["node"]["id"] for r in catalog if r["node"].get("image_url")}
    if set(checked) != expected_checked:
        raise ValueError("URL audit is incomplete or has extra product IDs")
    entries = []
    for slot, record in zip(slots, snap_records):
        p, row = by_row[slot["row"]], slot["row"]
        entries.append(dict(product_id=record["node"]["id"], source="musinsa_snap", row=row,
                            slot=slot["slot"], k=slot["k"], image_ref=IMAGE_BASE + str(row),
                            local_path=str(a.images / "snap" / f"{row}.jpg"), image_scope="full_snap",
                            multi_wearer_suspected=True if p["multi_wearer_suspected"] else None,
                            image_status=p["image_status"],
                            width=p["width"], height=p["height"]))
    # Catalog row is the v1 JSONL line order; file-local ID indices repeat across files.
    for catalog_row, record in enumerate(catalog):
        node = record["node"]
        url = node.get("image_url") or None
        check = checked.get(node["id"])
        if check and check["url"] != url:
            raise ValueError("URL audit target mismatch")
        status = check.get("image_status") if check else None
        if check and (check.get("get", {}).get("method") != "GET" or status not in {"ok", "broken", "unchecked"}):
            raise ValueError("URL receipt method/status differs from the G0-C2 contract")
        entries.append(dict(product_id=node["id"], source="otpensource", row=catalog_row,
                            slot=None, k=None, image_ref=url, local_path=None, image_scope="catalog",
                            multi_wearer_suspected=None, image_status=status, width=None, height=None))
    if len({e["product_id"] for e in entries}) != len(entries):
        raise ValueError("Index product IDs are not unique")
    if len({(s["row"], s["slot"], s["k"]) for s in slots}) != len(slots):
        raise ValueError("Snap (row,slot,k) mapping is not unique")
    if len({(e["row"], e["slot"], e["k"]) for e in entries}) != len(entries):
        raise ValueError("Index (row,slot,k) mapping is not unique")
    schema = pa.schema([(n, t) for n, t in [
        ("product_id", pa.string()), ("source", pa.string()), ("row", pa.int64()), ("slot", pa.string()),
        ("k", pa.int64()), ("image_ref", pa.string()), ("local_path", pa.string()), ("image_scope", pa.string()),
        ("multi_wearer_suspected", pa.bool_()), ("image_status", pa.string()), ("width", pa.int64()), ("height", pa.int64())]])
    table = pa.Table.from_pylist(entries, schema=schema)
    tmp = a.images / "index.parquet.partial"
    pq.write_table(table, tmp)
    reread = pq.read_table(tmp)
    if not table.equals(reread):
        raise ValueError("Index Parquet round-trip differs")
    # Every garment from one row must share the original image reference and path.
    for e in entries[:len(slots)]:
        if e["image_ref"] != IMAGE_BASE + str(e["row"]) or Path(e["local_path"]).name != f'{e["row"]}.jpg':
            raise ValueError("Full-snap reference invariant failed")
    tmp.replace(a.images / "index.parquet")
    summary = dict(rows=len(entries), sources=dict(Counter(e["source"] for e in entries)),
                   scopes=dict(Counter(e["image_scope"] for e in entries)),
                   image_status=dict(Counter(e["image_status"] for e in entries)),
                   suspected_snap_rows=sum(p["multi_wearer_suspected"] for p in photos),
                   suspected_products=sum(e["multi_wearer_suspected"] is True for e in entries),
                   row_semantics={"musinsa_snap": "pinned Parquet zero-based row", "otpensource": "v1 JSONL zero-based row"},
                   id_alignment_passed=report["passed"], index_sha256=sha256(a.images / "index.parquet"),
                   status_policy="G0-C2: streaming GET only; ok/broken/unchecked, no URL=null", checked_at=utc())
    atomic_json(a.images / "index_summary.json", summary)
    LOG.info("Final image index published: %s", summary)


def contact(a) -> None:
    import pyarrow.parquet as pq
    entries = pq.read_table(a.images / "index.parquet").to_pylist()
    records = load_jsonl(a.snap_jsonl) + load_jsonl(a.catalog_jsonl)
    categories = {r["node"]["id"]: " / ".join(str(v) for v in
                  (r.get("category_major"), r["node"].get("category_minor") or r["node"].get("category_raw")) if v)
                  for r in records}
    rng = random.Random(a.seed)
    # Sample distinct photos, not ten garments that could share one photo.
    groups = {}
    for source in ("musinsa_snap", "otpensource"):
        unique = {}
        for e in entries:
            if e["source"] == source and e["image_ref"]:
                unique.setdefault(e["image_ref"], e)
        groups[source] = rng.sample(list(unique.values()), 10)
    selected = groups["musinsa_snap"] + groups["otpensource"]
    cards = []
    for e in selected:
        src = "snap/" + Path(e["local_path"]).name if e["image_scope"] == "full_snap" else e["image_ref"]
        caption = f'{e["product_id"]} | {categories[e["product_id"]] or "분류 미상"} | {e["image_scope"]}'
        cards.append(f'<figure><img src="{html.escape(src, quote=True)}" alt="{html.escape(caption, quote=True)}" '
                     f'loading="eager" referrerpolicy="no-referrer"><figcaption>{html.escape(caption)}'
                     f'<br>{html.escape(e["image_status"] or "unchecked")}</figcaption></figure>')
    page = ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>C2 원본 사진 콘택트시트</title><style>body{font:15px sans-serif;margin:24px;background:#f5f5f5}'
            'main{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}'
            'figure{margin:0;padding:12px;background:white;border:1px solid #ddd}img{display:block;max-width:100%;'
            'max-height:430px;width:auto;height:auto;margin:auto}figcaption{overflow-wrap:anywhere;line-height:1.6}</style>'
            f'<h1>C2 원본 사진 20개</h1><p>03 10장 · 02 10장 · seed {a.seed} · 내부 연구용. '
            '03 의류는 같은 원본 사진을 공유하며 착용자 소속은 확정하지 않습니다. '
            '브라우저 화면 배치만 조정하며 이미지 파일 바이트는 변경하지 않습니다.</p><main>'
            + ''.join(cards) + '</main></html>')
    (a.images / "contact_sheet.html").write_text(page, encoding="utf-8")
    atomic_json(a.images / "contact_sheet_sample.json", dict(seed=a.seed, selected=selected, checked_at=utc()))
    LOG.info("Contact sheet generated: 10 full_snap + 10 catalog")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shared", type=Path, default=os.environ.get("SHARED"), required=not os.environ.get("SHARED"))
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--phase", choices=["download", "extract", "index", "contact", "all"], default="all")
    ap.add_argument("--parquet", type=Path)
    ap.add_argument("--snap-jsonl", type=Path)
    ap.add_argument("--catalog-jsonl", type=Path)
    ap.add_argument("--url-results", type=Path)
    ap.add_argument("--expected-sha256")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--expected-rows", type=int, default=9668)
    ap.add_argument("--expected-products", type=int, default=24532)
    ap.add_argument("--expected-catalog", type=int, default=8827)
    ap.add_argument("--seed", type=int, default=4335)
    a = ap.parse_args()
    a.shared = a.shared.resolve()
    a.images = a.shared / "images"
    a.images.mkdir(parents=True, exist_ok=True)
    a.parquet = a.parquet or a.shared / "hf" / FILENAME
    a.snap_jsonl = a.snap_jsonl or a.shared / "data/processed/v1/musinsa_snap_all.jsonl"
    a.catalog_jsonl = a.catalog_jsonl or a.shared / "data/processed/v1/otpensource_all.jsonl"
    a.url_results = a.url_results or a.images / "url_checks.jsonl"
    os.environ["HF_HOME"] = str(a.shared / "hf_cache")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(a.images / "run.log"), logging.StreamHandler()])
    phases = ["download", "extract", "index", "contact"] if a.phase == "all" else [a.phase]
    try:
        with exclusive(a.images / ".pipeline.lock"):
            LOG.info("START phases=%s df -h:\n%s", phases, subprocess.check_output(["df", "-h", str(a.shared)], text=True))
            for phase in phases:
                {"download": validate_download, "extract": extract, "index": build_index, "contact": contact}[phase](a)
            return 0
    except Exception as exc:
        # Do not log library exception text: it may contain a signed CDN URL.
        LOG.error("FAILED phases=%s exception=%s", phases, type(exc).__name__)
        if isinstance(exc, (ValueError, FileNotFoundError)):
            LOG.error("Detail: %s", exc)
        return 1
    finally:
        LOG.info("END df -h:\n%s", subprocess.check_output(["df", "-h", str(a.shared)], text=True))


if __name__ == "__main__":
    raise SystemExit(main())
