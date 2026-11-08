"""Resumable local-only embedding job: python -m trend_agent.embeddings.batch."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .runtime import DEFAULT_MODEL, FALLBACK_MODEL, SPECS, Encoder, checked_vectors, output_dir, shared_dir
from .text import LABELS, TEXT_RULES_VERSION, collect_inputs, preprocess_report, text_sha

LOG = logging.getLogger(__name__)
BASELINE_RUN = "20261107T171500Z-9e039290"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
PROBES = ["시럽코어", "보라색 니트", "절제된 맥시멀리즘", "드뮤어", "고프코어", "조용한 럭셔리",
          "발레코어", "미니멀리즘", "슬렌더 맨", "바랜 핑크", "오버사이즈 재킷", "블랙 가죽 재킷",
          "데님 팬츠", "레이스 블라우스", "체크 스커트", "퍼프 소매 원피스", "롱 코트", "크롭트 톱",
          "화이트 셔츠", "스포티한 아웃도어 룩"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, doc):
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def file_sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class BoardGuard:
    """Re-read live contracts/decisions and directed USER text before every phase."""
    def __init__(self, shared, out):
        self.path = shared.parent / "_vault/00_Team/BOARD.md"
        self.out = out
        self.baseline = None
        self.check("start")

    def check(self, phase):
        raw = self.path.read_text(encoding="utf-8")
        def section(key):
            return raw.split(f"<!-- {key}:BEGIN -->", 1)[1].split(f"<!-- {key}:END -->", 1)[0].strip()
        selected = {key: section(key) for key in ("CONTRACTS", "DECISIONS")}
        selected["USER"] = section("USER")  # Include multiline directives; conservative stop on any USER edit.
        outside_own = re.sub(r"<!-- C1:BEGIN -->.*?<!-- C1:END -->", "", raw, flags=re.S)
        selected["DIRECTED"] = [line for line in outside_own.splitlines() if re.search(r"@(C1|ALL)\b", line)]
        snapshot = {"at": now(), "phase": phase, "sections": selected}
        with (self.out / "board_checks.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        if self.baseline is not None and self.baseline != selected:
            raise RuntimeError("BOARD contract/decision/@C1 or @ALL changed; review required before resume")
        self.baseline = selected
        LOG.info("BOARD checked phase=%s", phase)


class VectorCache:
    """Document-only SQLite cache keyed by (model,text_sha), committed per batch."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (model TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (model TEXT, text_sha TEXT, vector BLOB NOT NULL, PRIMARY KEY(model,text_sha))")
        self.db.commit()

    def metadata(self, model):
        row = self.db.execute("SELECT data FROM metadata WHERE model=?", (model,)).fetchone()
        return json.loads(row[0]) if row else None

    def configure(self, encoder):
        meta = {"revision": encoder.revision, "dim": encoder.dim, "max_length": SPECS[encoder.model_name]["max_length"],
                "prefix": "passage: " if encoder.model_name == FALLBACK_MODEL else "", "dtype": "float32", "normalized": True}
        old = self.metadata(encoder.model_name)
        if old is not None and old != meta:
            raise RuntimeError("Cache model revision/settings changed; use a separate cache directory")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (encoder.model_name, json.dumps(meta, sort_keys=True)))

    def load(self, model, shas, dim):
        import numpy as np
        wanted = set(shas)
        result = {}
        for sha, blob in self.db.execute("SELECT text_sha,vector FROM vectors WHERE model=?", (model,)):
            if sha in wanted:
                vector = np.frombuffer(blob, dtype="<f4").copy()
                checked_vectors(vector.reshape(1, -1), 1, dim)
                result[sha] = vector
        return result

    def put(self, model, shas, vectors):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?,?)",
                                [(model, sha, v.astype("<f4").tobytes()) for sha, v in zip(shas, vectors)])


def event(shared, kind, summary, ref):
    payload = {"actor": "C1", "kind": kind, "to": ["T3", "T4", "L0"], "summary": summary, "ref": str(ref)}
    request = urllib.request.Request("http://127.0.0.1:8787/event", data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            result = {"via": "webhook", "response": response.read().decode()}
    except Exception as exc:
        LOG.warning("Webhook failed: %s; attempting protocol bus fallback", exc)
        cmd = ["python3", str(shared.parent / "_team/bus/emit.py"), "--kind", kind, "--to", "T3,T4,L0",
               "--summary", summary, "--ref", str(ref)]
        fallback = subprocess.run(cmd, env={**os.environ, "TEAM_AGENT": "C1"}, capture_output=True, text=True, timeout=15)
        result = {"via": "direct-fallback", "webhook_error": str(exc), "returncode": fallback.returncode,
                  "stdout": fallback.stdout, "stderr": fallback.stderr}
    with (output_dir() / "event_attempts.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": now(), "payload": payload, "result": result}, ensure_ascii=False) + "\n")


def validate(snapshot, rows, texts, dim):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    result = {}
    text_table = pq.read_table(snapshot / "text_index.parquet").to_pylist()
    if len(text_table) != len(texts) or {r["text_sha"]: r["text"] for r in text_table} != texts:
        raise ValueError("Text index count/content differs from input")
    if any(text_sha(r["text"]) != r["text_sha"] for r in text_table):
        raise ValueError("Invalid text SHA256")
    for label in LABELS:
        path = next(snapshot.glob(f"{label}_*.parquet"))
        table = pq.read_table(path)
        vector_type = table.schema.field("vector").type
        if not pa.types.is_fixed_size_list(vector_type) or vector_type.list_size != dim or vector_type.value_type != pa.float32():
            raise ValueError(f"{label}: vector column must be fixed_size_list<float32>[{dim}]")
        saved = table.select(["id", "text_sha"]).to_pylist()
        expected = [(r["id"], r["text_sha"]) for r in rows[label]]
        if [(r["id"], r["text_sha"]) for r in saved] != expected:
            raise ValueError(f"{label} row count/order/identity mismatch")
        vectors = table["vector"].combine_chunks().values.to_numpy().reshape(len(saved), dim)
        checked_vectors(vectors, len(saved), dim)
        norms = np.linalg.norm(vectors, axis=1)
        if len(norms) and not np.allclose(norms, 1, atol=2e-5):
            raise ValueError(f"{label}: vectors are not normalized")
        self_scores = []
        if len(saved):
            normalized = vectors / norms[:, None]
            for idx in sorted(set((0, len(saved) // 2, len(saved) - 1))):
                scores = normalized @ normalized[idx]
                if not np.isclose(scores[idx], 1.0, atol=2e-6) or not np.isclose(scores.max(), 1.0, atol=2e-6):
                    raise ValueError(f"{label}: self-search cosine != 1.0")
                self_scores.append(float(scores[idx]))
        result[label] = {"rows": len(saved), "expected_input_rows": len(rows[label]), "dimension": dim,
                         "nan_count": int(np.isnan(vectors).sum()), "inf_count": int(np.isinf(vectors).sum()),
                         "nonfinite_count": int((~np.isfinite(vectors)).sum()),
                         "self_search_cosines": self_scores, "sha256": file_sha(path)}
    return result


def probe_report(snapshot, rows, vectors, encoder, batch_size, baseline=None):
    import numpy as np
    import pyarrow.parquet as pq
    query_vectors = np.concatenate([encoder.encode(PROBES[i:i + batch_size]) for i in range(0, len(PROBES), batch_size)])
    lines = ["# C1 최근접 이웃 프로브", "", f"생성: {now()} · 모델: {encoder.model_name}@{encoder.revision}", "",
             "20개 질의 × Style/Term/Product/Item 상위 5. 코사인은 관련성 정답 판정이나 τ/δ 보정 결과가 아니다.", "",
             "| 질의 | 라벨 | 순위 | id | 코사인 | 표준 텍스트(최대 160자) |", "|---|---|---:|---|---:|---|"]
    def esc(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    matrices = {label: np.stack([vectors[r["text_sha"]] for r in rows[label]])
                for label in LABELS if rows[label]}
    for label, matrix in matrices.items():
        matrices[label] = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    current_hits = {}
    for query, vec in zip(PROBES, query_vectors):
        for label in ("style", "term", "product", "item"):
            entries = rows[label]
            if not entries:
                lines.append(f"| {esc(query)} | {label} | — | 입력 없음 | — | 빈 인덱스 |")
                continue
            scores = matrices[label] @ (vec / np.linalg.norm(vec))
            indices = np.lexsort((np.asarray([r["id"] for r in entries]), -scores))[:5]
            current_hits[query, label] = [(entries[idx]["id"], float(scores[idx])) for idx in indices]
            for rank, idx in enumerate(indices, 1):
                r = entries[idx]
                lines.append(f"| {esc(query)} | {label} | {rank} | {esc(r['id'])} | {scores[idx]:.6f} | {esc(r['text'][:160])} |")
    comparison = {"baseline_run": None, "probe_count": 10, "id_changes": {}}
    lines += ["", "## v1 대비 변화 — 같은 10개 프로브의 상위 5", ""]
    if baseline is None or not baseline.exists():
        lines += ["v1 baseline이 없어 비교 pending. 새 run의 검색 결과만 제시한다."]
        comparison["status"] = "pending_baseline"
    else:
        old = json.loads(baseline.read_text())
        if old["model"] != encoder.model_name or old["model_revision"] != encoder.revision or old["dimension"] != encoder.dim:
            raise ValueError("v1 comparison requires the same model revision and dimension")
        comparison.update(status="complete", baseline_run=old["run_id"])
        out = snapshot.parent.parent
        lines += [f"v1 `{old['run_id']}` ↔ 새 snapshot `{snapshot.name}`. 같은 질의 벡터와 코사인 계산을 사용한다.",
                  "입력 추가·제외와 전처리 효과가 함께 반영되므로 이 비교만으로 관련성 개선을 단정하지 않는다.", "",
                  "| 질의 | 라벨 | v1 상위 5 (id:cosine) | v2 상위 5 (id:cosine) | 공통 ID 수 |", "|---|---|---|---|---:|"]
        for label in LABELS:
            table = pq.read_table(out / old["outputs"][label]["path"])
            old_ids = np.asarray(table["id"].to_pylist(), dtype=str)
            old_shas = dict(zip(old_ids, table["text_sha"].to_pylist()))
            new_shas = {r["id"]: r["text_sha"] for r in rows[label]}
            common = old_shas.keys() & new_shas.keys()
            comparison["id_changes"][label] = {"v1": len(old_shas), "v2": len(new_shas),
                "added": len(new_shas.keys() - old_shas.keys()), "removed": len(old_shas.keys() - new_shas.keys()),
                "text_changed": sum(old_shas[k] != new_shas[k] for k in common),
                "text_unchanged": sum(old_shas[k] == new_shas[k] for k in common)}
            matrix = table["vector"].combine_chunks().values.to_numpy().reshape(len(old_ids), encoder.dim)
            if len(old_ids):
                matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
            for query, vec in zip(PROBES[:10], query_vectors[:10]):
                scores = matrix @ (vec / np.linalg.norm(vec))
                indices = np.lexsort((old_ids, -scores))[:5]
                old_hits = [(str(old_ids[idx]), float(scores[idx])) for idx in indices]
                new_hits = current_hits.get((query, label), [])
                def cell(hits):
                    return esc("; ".join(f"{node_id}:{score:.6f}" for node_id, score in hits) or "입력 없음")
                overlap = len({i for i, _ in old_hits} & {i for i, _ in new_hits})
                lines.append(f"| {esc(query)} | {label} | {cell(old_hits)} | {cell(new_hits)} | {overlap} |")
        lines += ["", "| 라벨 | v1 | v2 | 추가 | 제외 | 같은 ID 문장 변경 | 같은 ID 문장 유지 |", "|---|---:|---:|---:|---:|---:|---:|"]
        for label, c in comparison["id_changes"].items():
            lines.append(f"| {label} | " + " | ".join(str(c[k]) for k in ("v1", "v2", "added", "removed", "text_changed", "text_unchanged")) + " |")
    (snapshot / "probe_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    atomic_json(snapshot / "probe_comparison.json", comparison)
    return comparison


def check_inputs(shared, args, metadata, phase):
    """Recheck readiness and the complete file set, including newly added inputs."""
    _, current = collect_inputs(shared, product_version=args.product_version, extraction_version=args.extraction_version)
    if current != metadata:
        raise RuntimeError(f"Input/readiness changed before {phase}; rerun uses checkpoint cache")
    LOG.info("Inputs checked phase=%s ready=%s pending=%s", phase, current["ready"], current["pending"])


def run(args):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    shared, out = shared_dir(), output_dir()
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(shared / "hf_cache"))
    guard = BoardGuard(shared, out)
    started = time.monotonic()
    started_at = now()
    event(shared, "job_started", "C1 로컬 임베딩 배치 시작 (중복 제거·증분 체크포인트)", out / "run.log")
    guard.check("inventory")
    rows, metadata = collect_inputs(shared, product_version=args.product_version, extraction_version=args.extraction_version)
    texts = {r["text_sha"]: r["text"] for entries in rows.values() for r in entries}
    atomic_json(out / "input_inventory.json", metadata)
    (out / "preprocess_report.draft.md").write_text(preprocess_report(metadata), encoding="utf-8")
    if not texts:
        waiting = {"status": "pending", "text_rules_version": TEXT_RULES_VERSION, "at": now(),
                   "pending": metadata["pending"], "input": metadata, "newly_encoded": 0}
        atomic_json(out / "pending_manifest.json", waiting)
        atomic_json(out / "wait_state.json", waiting)
        LOG.info("No ready texts: preprocessing only; pending=%s", metadata["pending"])
        return
    if args.inventory_only:
        LOG.info("Inventory only: %s", metadata["statistics"])
        return
    if args.model != DEFAULT_MODEL:
        raise ValueError("c1-v2 requires BAAI/bge-m3 at the phase-1 revision")
    # Skip a completed publication only when inputs and verified immutable files still match.
    completion = out / "completion_state.json"
    if completion.exists() and (out / "manifest.json").exists():
        state = json.loads(completion.read_text())
        previous = json.loads((out / "manifest.json").read_text())
        if (state.get("status") == "complete" and state.get("manifest_sha256") == file_sha(out / "manifest.json")
                and previous.get("text_rules_version") == TEXT_RULES_VERSION and previous.get("input") == metadata):
            guard.check("resume-validate")
            snapshot = out / "runs" / previous["run_id"]
            if validate(snapshot, rows, texts, previous["dimension"]) != previous["validation"]:
                raise ValueError("Completed snapshot no longer matches validation")
            LOG.info("Already complete: %s; no encoding or republishing", previous["run_id"])
            return
    cache = VectorCache(out / "document_cache.sqlite3")
    model_failures = []
    selected = None
    for model in (args.model,):
        guard.check("model:" + model)
        old_meta = cache.metadata(model)
        try:
            if old_meta and old_meta["revision"] != MODEL_REVISION:
                raise RuntimeError("Cache revision differs from phase-1 BAAI/bge-m3 revision")
            encoder = Encoder(model, revision=MODEL_REVISION)
            cache.configure(encoder)
            vectors = cache.load(model, texts, encoder.dim)
            reused = len(vectors)
            pending = sorted(set(texts) - set(vectors))
            LOG.info("model=%s unique=%d cached=%d pending=%d", model, len(texts), reused, len(pending))
            for i in range(0, len(pending), args.batch_size):
                guard.check(f"encode:{model}:{i}")
                shas = pending[i:i + args.batch_size]
                matrix = encoder.encode([texts[sha] for sha in shas], documents=True)
                cache.put(model, shas, matrix)
                vectors.update(zip(shas, matrix))
                progress = {"status": "running", "at": now(), "model": model, "model_revision": encoder.revision,
                            "unique_texts": len(texts), "cache_reused": reused, "newly_encoded": i + len(shas),
                            "remaining": len(pending) - i - len(shas), "device": encoder.device,
                            "elapsed_seconds": time.monotonic() - started}
                atomic_json(out / "checkpoint.json", progress)
                LOG.info("Checkpoint %s", json.dumps(progress))
            selected = (encoder, vectors, reused, len(pending))
            break
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("BOARD "):
                cache.db.close()
                raise
            LOG.exception("Model failed: %s", model)
            model_failures.append({"model": model, "reason": str(exc), "at": now()})
            atomic_json(out / "model_failures.json", model_failures)
    cache.db.close()
    if selected is None:
        raise RuntimeError(f"All local model attempts failed: {model_failures}")
    encoder, vectors, reused, newly_encoded = selected
    document_truncated = encoder.truncated_texts
    guard.check("export")
    check_inputs(shared, args, metadata, "export")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    snapshot = out / "runs" / run_id
    snapshot.mkdir(parents=True)
    (snapshot / "preprocess_report.md").write_text(preprocess_report(metadata), encoding="utf-8")
    slug = encoder.model_name.replace("/", "--")
    schema = pa.schema([("id", pa.string()), ("text_sha", pa.string()), ("vector", pa.list_(pa.float32(), encoder.dim))])
    outputs = {}
    for label in LABELS:
        name = f"{label}_{slug}_{encoder.dim}.parquet"
        entries = [{"id": r["id"], "text_sha": r["text_sha"], "vector": vectors[r["text_sha"]].tolist()} for r in rows[label]]
        pq.write_table(pa.Table.from_pylist(entries, schema=schema), snapshot / name, compression="zstd")
        outputs[label] = {"path": str((snapshot / name).relative_to(out)), "rows": len(entries),
                          "unique_texts": len({r["text_sha"] for r in rows[label]})}
    index_schema = pa.schema([("text_sha", pa.string()), ("text", pa.string())])
    pq.write_table(pa.Table.from_pylist([{"text_sha": sha, "text": texts[sha]} for sha in sorted(texts)], schema=index_schema),
                   snapshot / "text_index.parquet", compression="zstd")
    guard.check("validation")
    check_inputs(shared, args, metadata, "validation")
    validation = validate(snapshot, rows, texts, encoder.dim)
    guard.check("probes")
    baseline = out / "runs" / getattr(args, "baseline_run", BASELINE_RUN) / "manifest.json"
    comparison = probe_report(snapshot, rows, vectors, encoder, args.batch_size, baseline)
    guard.check("publish")
    check_inputs(shared, args, metadata, "publish")
    manifest = {"status": "ready", "run_id": run_id, "model": encoder.model_name, "model_revision": encoder.revision,
                "dimension": encoder.dim, "license": "MIT", "license_source": f"https://huggingface.co/{encoder.model_name}/raw/main/README.md",
                "text_rules_version": TEXT_RULES_VERSION, "started_at": started_at, "generated_at": now(),
                "device": encoder.device, "mps_available": encoder.mps_available, "device_failures": encoder.failures,
                "model_failures": model_failures, "elapsed_seconds": time.monotonic() - started,
                "load_seconds": encoder.load_seconds, "encoding_seconds_by_device": encoder.timings,
                "unique_texts": len(texts), "cache_reused": reused, "newly_encoded": newly_encoded,
                "computed_texts": newly_encoded, "pending": metadata["pending"],
                "new_document_truncated_texts": document_truncated, "max_length": SPECS[encoder.model_name]["max_length"],
                "outputs": outputs, "text_index": str((snapshot / "text_index.parquet").relative_to(out)),
                "probe_report": str((snapshot / "probe_report.md").relative_to(out)), "probe_queries": PROBES,
                "preprocess_report": str((snapshot / "preprocess_report.md").relative_to(out)),
                "probe_comparison": comparison,
                "input": metadata, "validation": validation,
                "packages": {p: importlib.metadata.version(p) for p in ("numpy", "pyarrow", "torch", "sentence-transformers")}}
    atomic_json(snapshot / "manifest.json", manifest)
    # Readers follow immutable paths from the atomic manifest; conventional root aliases are for manual inspection.
    for path in snapshot.iterdir():
        if path.name == "manifest.json":
            continue
        alias = out / path.name
        tmp = out / ("." + path.name + "." + run_id)
        tmp.symlink_to(path.relative_to(out))
        os.replace(tmp, alias)
    atomic_json(out / "manifest.json", manifest)
    atomic_json(out / "checkpoint.json", {"status": "ready", "manifest": str(out / "manifest.json"), "at": now()})
    LOG.info("READY: %s", json.dumps(manifest, ensure_ascii=False))
    # C1 publishes job_done only after final review, BOARD summary, and T3/T4 handoff.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-version", choices=["v1", "v2"])
    parser.add_argument("--extraction-version")
    parser.add_argument("--model", choices=list(SPECS), default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--baseline-run", default=BASELINE_RUN, help="Immutable v1 run ID for probe comparison")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("batch-size must be positive")
    if Path(args.baseline_run).name != args.baseline_run:
        parser.error("baseline-run must be a run ID, not a path")
    out = output_dir()
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(out / "run.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    with (out / ".batch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("Another C1 batch owns the lock")
            return 2
        try:
            run(args)
        except Exception as exc:
            LOG.exception("C1 embedding job failed")
            atomic_json(out / "last_failure.json", {"status": "failed", "at": now(), "error": str(exc)})
            event(shared_dir(), "job_failed", f"C1 배치 실패: {exc}; 체크포인트 유지", out / "last_failure.json")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
