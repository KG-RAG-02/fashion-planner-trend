"""Audit catalog image URLs with streaming GET headers only (G0-C2).

No HEAD, Range, response-body reads, or image downloads. At most 8 workers,
10s timeout, and 2 retries. Append-only receipts resume the same input/policy.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
try:
    import fcntl

    def _lock_nb(f) -> None:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except ImportError:  # Windows
    import msvcrt

    def _lock_nb(f) -> None:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from None
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = "VOGUE-Research-ImageAudit/1.0 (internal image availability check)"
POLICY = dict(timeout_seconds=10, retries=2, max_workers=8, user_agent=USER_AGENT,
              method="GET", body="not_read", range_header=False, version=2)
LOG = logging.getLogger("C2.urls")


def _disk(path) -> str:
    """디스크 용량 요약. `df -h`는 Windows에 없어서 shutil로 대신한다."""
    import shutil
    u = shutil.disk_usage(path)
    return f"total {u.total / 1e9:.0f}GB used {u.used / 1e9:.0f}GB free {u.free / 1e9:.0f}GB ({path})"


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, obj):
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def request(url: str) -> dict:
    """Return HTTP and transport outcomes separately; close GET before reading body."""
    started = time.monotonic()
    result = dict(method="GET", status_code=None, content_type=None, error=None,
                  attempts=0, checked_at=utc())
    if urlparse(url).scheme not in {"https", "http"}:
        return dict(result, error="invalid_scheme", elapsed_ms=0)
    for attempt in range(3):
        result.update(status_code=None, content_type=None, error=None, attempts=attempt + 1)
        try:
            req = Request(url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5"})
            with urlopen(req, timeout=10) as response:
                result.update(status_code=response.status, content_type=response.headers.get("Content-Type"))
                # No response.read(), no Range header, no image bytes saved.
        except HTTPError as exc:
            result.update(status_code=exc.code, content_type=exc.headers.get("Content-Type"))
            exc.close()
        except (TimeoutError, socket.timeout):
            result["error"] = "timeout"
        except URLError as exc:
            reason = exc.reason
            result["error"] = ("dns_error" if isinstance(reason, socket.gaierror) else
                               "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else
                               "tls_error" if isinstance(reason, ssl.SSLError) else "transport_error")
        except ssl.SSLError:
            result["error"] = "tls_error"
        except OSError:
            result["error"] = "transport_error"
        retry = result["error"] is not None or result["status_code"] in {408, 429, 500, 502, 503, 504}
        if not retry or attempt == 2:
            break
        time.sleep(0.5 * (attempt + 1))
    return dict(result, elapsed_ms=round((time.monotonic() - started) * 1000))


def catalog_status(result: dict) -> str:
    """G0-C2 display status. A transient failure never proves a broken image."""
    code = result.get("status_code")
    mime = (result.get("content_type") or "").split(";", 1)[0].strip().lower()
    if result.get("error") or code is None or code >= 500:
        return "unchecked"
    if code in {403, 404, 410}:
        return "broken"
    if 200 <= code < 300 and mime.startswith("image/"):
        return "ok"
    if mime and not mime.startswith("image/"):
        return "broken"
    return "unchecked"


def check_one(item) -> dict:
    product_id, url = item
    get = request(url)
    return dict(product_id=product_id, url=url, get=get, image_status=catalog_status(get))


def read_receipts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    end = raw.rfind(b"\n") + 1
    if end != len(raw):
        # A terminated writer can leave one incomplete trailing record.
        with path.open("r+b") as f:
            f.truncate(end)
    return [json.loads(line) for line in raw[:end].splitlines() if line.strip()]


def audit(a) -> int:
    input_bytes = a.input.read_bytes()
    records = [json.loads(line) for line in input_bytes.splitlines() if line.strip()]
    items = [(r["node"]["id"], r["node"]["image_url"]) for r in records if r["node"].get("image_url")]
    if len(items) != a.expected_urls or len({key for key, _ in items}) != len(items):
        raise ValueError("URL row count or product ID uniqueness differs from expected input")
    identity = dict(input_sha256=hashlib.sha256(input_bytes).hexdigest(), policy=POLICY)
    meta_path, result_path = a.images / "url_checks_meta.json", a.images / "url_checks.jsonl"
    if meta_path.exists():
        old = json.loads(meta_path.read_text())
        if any(old.get(k) != v for k, v in identity.items()):
            raise ValueError("Resume input/policy differs; use a separate output directory")
    elif result_path.exists() and result_path.stat().st_size:
        raise ValueError("URL receipts exist without their input/policy metadata")
    atomic_json(meta_path, dict(identity, total_products=len(records), url_rows=len(items),
                               unique_urls=len({u for _, u in items})))
    existing = read_receipts(result_path)
    completed = {r["product_id"]: r for r in existing}
    expected = dict(items)
    if len(completed) != len(existing) or any(expected.get(k) != r["url"] for k, r in completed.items()):
        raise ValueError("URL receipts have duplicate IDs or different targets")
    pending = [item for item in items if item[0] not in completed]
    if pending:
        # Stop early on a globally unusable network; do not invent 8,762 HTTP outcomes.
        probe = check_one(pending[0])
        atomic_json(a.images / "url_probe.json", probe)
        if probe["get"]["error"] in {"dns_error", "transport_error", "tls_error"}:
            atomic_json(a.images / "url_checks_summary.json", dict(
                complete=False, url_rows=len(items), resumed_rows=len(completed),
                bulk_attempted=False, probe_error=probe["get"]["error"], probe_attempts=probe["get"]["attempts"],
                checked_at=utc(), note="Network probe failed; no HTTP status distribution inferred for pending URLs"))
            raise ConnectionError(f'URL network probe failed: {probe["get"]["error"]}')
        with result_path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(probe, ensure_ascii=False) + "\n")
            out.flush()
            completed[probe["product_id"]] = probe
            with ThreadPoolExecutor(max_workers=a.workers) as pool:
                futures = [pool.submit(check_one, item) for item in pending[1:]]
                for future in as_completed(futures):
                    receipt = future.result()
                    out.write(json.dumps(receipt, ensure_ascii=False) + "\n")
                    out.flush()
                    completed[receipt["product_id"]] = receipt
                    if len(completed) % 250 == 0:
                        LOG.info("URL GET receipts %s/%s", len(completed), len(items))
    receipts = list(completed.values())
    distribution = Counter(str(r["get"]["status_code"]) if r["get"]["status_code"] is not None
                           else r["get"]["error"] for r in receipts)
    summary = dict(complete=len(completed) == len(items), url_rows=len(items), unique_urls=len(set(expected.values())),
                   completed_rows=len(completed), missing_url_rows=len(records) - len(items),
                   get_status_distribution=dict(distribution),
                   image_status_distribution=dict(Counter(r["image_status"] for r in receipts)),
                   get_request_attempts=sum(r["get"]["attempts"] for r in receipts),
                   policy=POLICY, input_sha256=identity["input_sha256"], checked_at=utc())
    atomic_json(a.images / "url_checks_summary.json", summary)
    LOG.info("URL audit summary: %s", summary)
    return 0 if summary["complete"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shared", type=Path, default=os.environ.get("SHARED"), required=not os.environ.get("SHARED"))
    ap.add_argument("--input", type=Path)
    ap.add_argument("--workers", type=int, choices=range(1, 9), default=8)
    ap.add_argument("--expected-urls", type=int, default=8762)
    a = ap.parse_args()
    a.shared = a.shared.resolve()
    a.images = a.shared / "images"
    a.images.mkdir(parents=True, exist_ok=True)
    a.input = a.input or a.shared / "data/processed/v1/otpensource_all.jsonl"
    os.environ["HF_HOME"] = str(a.shared / "hf_cache")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(a.images / "run.log"), logging.StreamHandler()])
    try:
        with (a.images / ".url_checks.lock").open("a") as lock:
            _lock_nb(lock)
            LOG.info("START streaming GET audit df -h:\n%s", _disk(a.shared))
            return audit(a)
    except Exception as exc:
        LOG.error("URL audit FAILED exception=%s", type(exc).__name__)
        if isinstance(exc, (ValueError, FileNotFoundError, ConnectionError)):
            LOG.error("Detail: %s", exc)
        return 1
    finally:
        LOG.info("END df -h:\n%s", _disk(a.shared))


if __name__ == "__main__":
    raise SystemExit(main())
