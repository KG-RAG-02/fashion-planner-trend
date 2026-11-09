#!/usr/bin/env python3
"""Resumable, local-only catalog content audit. Never modifies the image index.

Images are GET entity bytes, not rendered copies. CLIP's tensor preprocessing is
in memory only; no crop, thumbnail, re-encoded or resized image is written.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import subprocess
import time

MODEL = "openai/clip-vit-base-patch32"
REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
PROMPTS = {
    "garment_product": "a product photo of a garment",
    "garment_worn": "a person wearing clothes",
    "garment_flat": "clothing laid flat",
    "care_label": "a clothing care label close-up",
    "size_chart": "a size chart or table",
    "text_notice": "a text banner or notice",
    "fabric_closeup": "a fabric texture close-up",
    "accessory": "shoes or a bag",
    "brand_logo": "a brand logo",
}
GARMENT_LABELS = set(list(PROMPTS)[:3])
UA = "VOGUE-Research-CatalogContent/2.0 (internal research image audit)"


def _disk(path) -> str:
    """디스크 용량 요약. `df -h`는 Windows에 없어서 shutil로 대신한다."""
    import shutil
    u = shutil.disk_usage(path)
    return f"total {u.total / 1e9:.0f}GB used {u.used / 1e9:.0f}GB free {u.free / 1e9:.0f}GB ({path})"


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def atomic_json(path, obj):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def read_jsonl(path):
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A killed writer can leave just its last line incomplete.
            if line != Path(path).read_text().splitlines()[-1]:
                raise
    return rows


def append_jsonl(path, rows):
    # Main thread is the only receipt/checkpoint writer.
    path = Path(path)
    if path.exists() and path.stat().st_size:
        with path.open("rb+") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                data = path.read_bytes()
                offset = data.rfind(b"\n") + 1
                try:
                    json.loads(data[offset:])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    f.truncate(offset)
                else:
                    f.seek(0, 2)
                    f.write(b"\n")
    with path.open("a") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def inputs(shared):
    import pyarrow.parquet as pq
    frame = pq.read_table(shared / "images/index.parquet").to_pandas()
    frame = frame[(frame.source == "otpensource") & (frame.image_status == "ok")]
    rows = frame.to_dict("records")
    urls = {}
    for row in rows:
        url = row["image_ref"]
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        if key in urls and urls[key] != url:
            raise ValueError("Truncated URL SHA collision")
        urls[key] = url
    return rows, urls


def fetch_one(key, url, dest):
    import requests
    from PIL import Image
    last = None
    for attempt in range(1, 4):
        part = dest / (key + ".part")
        rec = dict(url_key=key, url=url, checked_at=now(), attempts=attempt,
                   method="GET", range_header=False, fetch_status="transport_error",
                   local_path=None, http_status=None, user_agent=UA)
        try:
            with requests.get(url, stream=True, timeout=(10, 10), headers={
                "User-Agent": UA, "Accept-Encoding": "identity",
            }) as response:
                rec.update(http_status=response.status_code,
                           mime=response.headers.get("Content-Type"),
                           content_length=response.headers.get("Content-Length"),
                           content_encoding=response.headers.get("Content-Encoding"),
                           final_url=response.url)
                if response.status_code >= 500 or response.status_code == 429:
                    rec["fetch_status"] = "http_error"
                    last = rec
                    continue
                if not 200 <= response.status_code < 300:
                    rec["fetch_status"] = "http_error"
                    return rec
                h, count = hashlib.sha256(), 0
                with part.open("wb") as f:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            f.write(chunk)
                            h.update(chunk)
                            count += len(chunk)
                with Image.open(part) as im:
                    width, height, fmt = im.width, im.height, im.format
                    im.verify()
                ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp",
                       "GIF": "gif", "BMP": "bmp", "TIFF": "tif"}.get(fmt)
                if not ext:
                    raise ValueError(f"Unsupported image format: {fmt}")
                if rec["content_length"] and not rec["content_encoding"]:
                    if count != int(rec["content_length"]):
                        raise ValueError("Content-Length mismatch")
                path = dest / f"{key}.{ext}"
                part.replace(path)
                rec.update(fetch_status="ok", local_path=str(path), bytes=count,
                           sha256=h.hexdigest(), width=width, height=height,
                           format=fmt, byte_policy="unchanged_GET_entity_bytes")
                return rec
        except Exception as exc:
            rec.update(error=f"{type(exc).__name__}: {exc}", fetch_status="fetch_error")
            last = rec
        finally:
            if part.exists():
                part.unlink()
        if attempt < 3:
            time.sleep(attempt)
    return last


def fetch(shared):
    rows, urls = inputs(shared)
    dest = shared / "images/catalog"
    dest.mkdir(exist_ok=True)
    receipts = shared / "preprocess/catalog_fetch.jsonl"
    existing = {r["url_key"]: r for r in read_jsonl(receipts)}
    todo = []
    for key, url in urls.items():
        prev = existing.get(key)
        if prev and prev["url"] == url:
            if prev["fetch_status"] != "ok":
                continue
            path = Path(prev["local_path"])
            if path.is_file() and sha(path) == prev["sha256"]:
                continue
        todo.append((key, url))
    logging.info("fetch: products=%d unique_urls=%d remaining=%d", len(rows), len(urls), len(todo))
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(fetch_one, k, u, dest) for k, u in todo]
        for i, job in enumerate(as_completed(jobs), 1):
            rec = job.result()
            append_jsonl(receipts, [rec])
            if i % 250 == 0 or i == len(jobs):
                logging.info("fetch progress %d/%d", i, len(jobs))
    data = {r["url_key"]: r for r in read_jsonl(receipts)}
    from collections import Counter
    summary = dict(at=now(), product_rows=len(rows), unique_urls=len(urls),
                   fetch_status=dict(Counter(data[k]["fetch_status"] for k in urls)),
                   bytes=sum(data[k].get("bytes", 0) for k in urls),
                   input_sha256=sha(shared / "images/index.parquet"),
                   policy=dict(method="GET", workers=8, timeout_seconds=10,
                               retries=2, range_header=False, user_agent=UA,
                               transformations="none"))
    atomic_json(shared / "preprocess/catalog_fetch_summary.json", summary)
    logging.info("fetch complete %s", summary)


def score(shared, batch_size):
    import requests
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor
    from huggingface_hub import snapshot_download
    p = shared / "preprocess"
    meta_path = p / "content_model.json"
    if not meta_path.exists():
        api_url = f"https://huggingface.co/api/models/{MODEL}/revision/{REVISION}"
        resp = requests.get(api_url, timeout=20)
        resp.raise_for_status()
        api = resp.json()
        assert api["sha"] == REVISION
        card = requests.get(f"https://huggingface.co/{MODEL}/raw/{REVISION}/README.md", timeout=20)
        card.raise_for_status()
        (p / "clip_model_card.md").write_text(card.text)
        atomic_json(meta_path, dict(model=MODEL, revision=REVISION, at=now(),
            card_license=api.get("cardData", {}).get("license"),
            card_license_note="HF model card does not declare a license field; upstream CLIP is MIT",
            upstream_license="MIT", license_url="https://github.com/openai/CLIP/blob/main/LICENSE",
            model_card_url=f"https://huggingface.co/{MODEL}/blob/{REVISION}/README.md",
            prompts=PROMPTS, probability_method="softmax(CLIP learned logit_scale * cosine), 9 prompts",
            binary_score="max(3 garment probabilities)/(max(3 garment)+max(6 non-garment))",
            tensor_preprocessing="in-memory CLIP 224x224 full image; center crop disabled; no derived image files",
            api=api))
    snapshot = snapshot_download(MODEL, revision=REVISION,
        allow_patterns=["*.json", "merges.txt", "vocab.json", "pytorch_model.bin", "README.md"])
    processor = CLIPProcessor.from_pretrained(snapshot, local_files_only=True)
    processor.image_processor.do_center_crop = False
    processor.image_processor.size = {"height": 224, "width": 224}
    torch.set_num_threads(4)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = CLIPModel.from_pretrained(snapshot, local_files_only=True).eval().to(device)
    names = list(PROMPTS)
    text_inputs = processor(text=list(PROMPTS.values()), return_tensors="pt", padding=True)
    def text_embeddings():
        emb = model.get_text_features(**{k: v.to(device) for k, v in text_inputs.items()})
        if not isinstance(emb, torch.Tensor):
            emb = emb.pooler_output
        return emb / emb.norm(dim=-1, keepdim=True)
    with torch.inference_mode():
        try:
            texts = text_embeddings()
        except RuntimeError:
            if device != "mps":
                raise
            device = "cpu"
            model.to(device)
            texts = text_embeddings()
    metadata = json.loads(meta_path.read_text())
    metadata.update(device=device, torch_version=torch.__version__, batch_size=batch_size)
    atomic_json(meta_path, metadata)
    rows, urls = inputs(shared)
    receipts = {r["url_key"]: r for r in read_jsonl(p / "catalog_fetch.jsonl")}
    score_file = p / "catalog_scores.jsonl"
    prior = {r["url_key"]: r for r in read_jsonl(score_file)}
    todo = [r for k in urls if k in receipts and (r := receipts[k])["fetch_status"] == "ok" and
            not (k in prior and prior[k].get("image_sha256") == r["sha256"] and
                 prior[k].get("model_revision") == REVISION)]
    logging.info("score device=%s remaining=%d", device, len(todo))
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        images = []
        valid = []
        for rec in batch:
            try:
                with Image.open(rec["local_path"]) as im:
                    images.append(im.convert("RGB"))
                valid.append(rec)
            except Exception as exc:
                append_jsonl(score_file, [dict(url_key=rec["url_key"], model_revision=REVISION,
                    image_sha256=rec["sha256"], score_status="decode_error", error=str(exc))])
        if not valid:
            continue
        pixel_inputs = processor(images=images, return_tensors="pt")
        with torch.inference_mode():
            try:
                features = model.get_image_features(pixel_values=pixel_inputs.pixel_values.to(device))
            except RuntimeError:
                if device != "mps":
                    raise
                device = "cpu"
                model.to(device)
                texts = text_embeddings()
                features = model.get_image_features(pixel_values=pixel_inputs.pixel_values.to(device))
                metadata["device_fallback"] = "cpu"
                atomic_json(meta_path, metadata)
            if not isinstance(features, torch.Tensor):
                features = features.pooler_output
            features = features / features.norm(dim=-1, keepdim=True)
            probs = (model.logit_scale.exp() * features @ texts.T).softmax(dim=-1).cpu().tolist()
        output = []
        for rec, values in zip(valid, probs):
            vals = dict(zip(names, values))
            g, n = max(values[:3]), max(values[3:])
            output.append(dict(url_key=rec["url_key"], local_path=rec["local_path"],
                image_sha256=rec["sha256"], score_status="ok", scores=vals,
                top_label=max(vals, key=vals.get), garment_score=g / (g + n),
                model=MODEL, model_revision=REVISION, device=device, at=now()))
        append_jsonl(score_file, output)
        for im in images:
            im.close()
        if start // batch_size % 10 == 0 or start + batch_size >= len(todo):
            logging.info("score progress %d/%d", min(start + batch_size, len(todo)), len(todo))
    logging.info("score finished")


def sample(shared):
    import numpy as np
    p = shared / "preprocess"
    path = p / "content_review_sample.jsonl"
    if path.exists():
        logging.info("sample already frozen; unchanged")
        return
    scores = [r for r in read_jsonl(p / "catalog_scores.jsonl") if r["score_status"] == "ok"]
    scores = list({r["url_key"]: r for r in scores}.values())
    _, urls = inputs(shared)
    fetched = {r["url_key"]: r for r in read_jsonl(p / "catalog_fetch.jsonl")}
    scored_keys = {r["url_key"] for r in read_jsonl(p / "catalog_scores.jsonl")}
    assert set(fetched) == set(urls), "Finish the full URL cache before freezing a sample"
    assert all(k in scored_keys for k, r in fetched.items() if r["fetch_status"] == "ok"), "Finish scoring before freezing a sample"
    # Distribution-derived provisional abstention band, before seeing any labels.
    q = float(np.quantile([abs(r["garment_score"] - .5) for r in scores], .2))
    lo, hi = .5 - q, .5 + q
    groups = {"non_garment": [], "uncertain": [], "garment": []}
    for r in scores:
        g = r["garment_score"]
        kind = "non_garment" if g <= lo else "garment" if g >= hi else "uncertain"
        r["initial_class"] = kind
        groups[kind].append(r)
    rng = random.Random(20261108)
    chosen = []
    for kind, count in [("non_garment", 60), ("uncertain", 30), ("garment", 30)]:
        group = sorted(groups[kind], key=lambda r: r["url_key"])
        picked = rng.sample(group, min(count, len(group)))
        for r in picked:
            chosen.append({**r, "sample_stratum": kind, "population_stratum_n": len(group),
                           "sample_stratum_n": len(picked), "review_id": len(chosen) + 1})
    if len(chosen) < 120:
        seen = {r["url_key"] for r in chosen}
        extra = rng.sample([r for r in groups["garment"] if r["url_key"] not in seen], 120 - len(chosen))
        for r in extra:
            chosen.append({**r, "sample_stratum": "garment", "population_stratum_n": len(groups["garment"]),
                           "review_id": len(chosen) + 1})
        n = sum(r["sample_stratum"] == "garment" for r in chosen)
        for r in chosen:
            if r["sample_stratum"] == "garment":
                r["sample_stratum_n"] = n
    for kind in groups:
        subset = [r for r in chosen if r["sample_stratum"] == kind]
        # Freeze the calibration/evaluation split before any visual labels exist.
        for i, r in enumerate(subset):
            r["review_partition"] = "holdout" if i % 3 == 2 else "calibration"
    append_jsonl(path, chosen)
    atomic_json(p / "content_initial_thresholds.json", dict(at=now(), seed=20261108,
        method="bottom 20% absolute distance to binary argmax boundary becomes uncertain; data quantile, no review labels",
        low=lo, high=hi, population={k: len(v) for k, v in groups.items()},
        sample={k: sum(r["sample_stratum"] == k for r in chosen) for k in groups},
        input_scores_sha256=sha(p / "catalog_scores.jsonl")))
    logging.info("sample frozen: %s", {k: sum(r["sample_stratum"] == k for r in chosen) for k in groups})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--phase", choices=["fetch", "score", "sample"], required=True)
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()
    shared = args.shared.resolve()
    (shared / "preprocess").mkdir(exist_ok=True)
    os.environ["HF_HOME"] = str(shared / "hf_cache")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(shared / "preprocess/run.log"), logging.StreamHandler()])
    logging.info("START %s df -h:\n%s", args.phase,
        _disk(shared))
    if args.phase == "score":
        score(shared, args.batch_size)
    else:
        globals()[args.phase](shared)
    logging.info("END %s df -h:\n%s", args.phase,
        _disk(shared))


if __name__ == "__main__":
    main()
