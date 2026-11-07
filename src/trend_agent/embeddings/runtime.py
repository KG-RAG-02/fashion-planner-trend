"""Local dense encoders and manifest-backed numpy cosine search. No paid APIs."""
from __future__ import annotations

import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

DEFAULT_MODEL = "BAAI/bge-m3"
FALLBACK_MODEL = "intfloat/multilingual-e5-large"
SPECS = {DEFAULT_MODEL: {"dim": 1024, "max_length": 8192}, FALLBACK_MODEL: {"dim": 1024, "max_length": 512}}
LOGGER = logging.getLogger(__name__)


def shared_dir() -> Path:
    configured = os.environ.get("SHARED")
    if configured:
        return Path(configured).expanduser().resolve()
    # Source may live in VOGUE/worktrees/c1 or VOGUE/trend-analysis-agent.
    for parent in Path(__file__).resolve().parents:
        if (parent / "_vault/00_Team/PROTOCOL.md").exists():
            return parent / "_shared"
    raise RuntimeError("Set SHARED to the VOGUE shared directory")


def output_dir() -> Path:
    value = os.environ.get("VOGUE_EMBEDDINGS_DIR")
    return Path(value).expanduser().resolve() if value else shared_dir() / "embeddings"


def read_manifest() -> dict:
    path = output_dir() / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "ready":
        raise RuntimeError(f"Embedding manifest is not ready: {path}")
    if manifest.get("model") not in SPECS:
        raise ValueError("Manifest model is not an approved local model")
    if manifest.get("dimension") != SPECS[manifest["model"]]["dim"]:
        raise ValueError("Manifest dimension does not match model")
    return manifest


class Encoder:
    def __init__(self, model: str, *, revision: str | None = None):
        if model not in SPECS:
            raise ValueError(f"Unsupported local model: {model}")
        os.environ.setdefault("HF_HOME", str(shared_dir() / "hf_cache"))
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        import torch
        from huggingface_hub import HfApi, try_to_load_from_cache
        from sentence_transformers import SentenceTransformer

        self.model_name = model
        cached_config = try_to_load_from_cache(model, "config.json") if revision is None else None
        cached_revision = Path(cached_config).parent.name if isinstance(cached_config, str) else None
        self.revision = revision or cached_revision or HfApi().model_info(model).sha
        self.dim = SPECS[model]["dim"]
        self.failures = []
        self.timings = {"mps": 0.0, "cpu": 0.0}
        self.truncated_texts = 0
        self.mps_available = bool(torch.backends.mps.is_available())
        self.device = "mps" if self.mps_available else "cpu"
        torch.set_num_threads(max(1, int(os.environ.get("C1_TORCH_THREADS", "4"))))
        started = time.monotonic()
        try:
            self.encoder = SentenceTransformer(model, device=self.device, revision=self.revision, trust_remote_code=False)
        except Exception as exc:
            if self.device != "mps":
                raise
            self.failures.append({"device": "mps", "phase": "load", "reason": str(exc)})
            self.timings["mps"] += time.monotonic() - started
            LOGGER.warning("MPS load failed, retrying CPU: %s", exc)
            self.device = "cpu"
            self.encoder = SentenceTransformer(model, device="cpu", revision=self.revision, trust_remote_code=False)
        self.encoder.max_seq_length = SPECS[model]["max_length"]
        self.load_seconds = time.monotonic() - started
        LOGGER.info("Loaded %s@%s device=%s seconds=%.2f", model, self.revision, self.device, self.load_seconds)

    def encode(self, texts: list[str], *, documents=False):
        import numpy as np
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("Encoder requires nonempty strings")
        prefix = ("passage: " if documents else "query: ") if self.model_name == FALLBACK_MODEL else ""
        prepared = [prefix + t for t in texts]
        tokens = self.encoder.tokenizer(prepared, truncation=False, padding=False, verbose=False)
        truncated = sum(len(t) > self.encoder.max_seq_length for t in tokens["input_ids"])
        self.truncated_texts += truncated
        device = self.device
        started = time.monotonic()
        try:
            vecs = self.encoder.encode(prepared, batch_size=len(prepared), show_progress_bar=False,
                                       convert_to_numpy=True, normalize_embeddings=True, prompt="")
            vecs = checked_vectors(vecs, len(texts), self.dim)
        except Exception as exc:
            self.timings[device] += time.monotonic() - started
            if device != "mps":
                raise
            self.failures.append({"device": "mps", "phase": "encode", "reason": str(exc)})
            LOGGER.warning("MPS encode failed, retrying batch on CPU: %s", exc)
            self.encoder.to("cpu")
            self.device = "cpu"
            self.truncated_texts -= truncated  # Retry counts each text once.
            return self.encode(texts, documents=documents)
        self.timings[device] += time.monotonic() - started
        return vecs


def checked_vectors(vectors, count: int, dim: int):
    import numpy as np
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.shape != (count, dim):
        raise ValueError(f"Vector shape {matrix.shape} != {(count, dim)}")
    if not np.isfinite(matrix).all():
        raise ValueError("Non-finite vectors")
    if count and np.any(np.linalg.norm(matrix, axis=1) <= 0):
        raise ValueError("Zero vectors")
    return matrix


@lru_cache(maxsize=1)
def _query_encoder(model, revision):
    return Encoder(model, revision=revision)


def embed_texts(texts: list[str], model: str | None = None) -> np.ndarray:
    if not isinstance(texts, list):
        raise TypeError("texts must be list[str]")
    revision = None
    if (output_dir() / "manifest.json").exists():
        manifest = read_manifest()
        model = model or manifest["model"]
        if manifest["model"] == model:
            revision = manifest["model_revision"]
    model = model or DEFAULT_MODEL
    if model not in SPECS:
        raise ValueError(f"Unsupported local model: {model}")
    if not texts:
        import numpy as np
        return np.empty((0, SPECS[model]["dim"]), dtype=np.float32)
    import numpy as np
    encoder = _query_encoder(model, revision)
    batch_size = max(1, int(os.environ.get("C1_EMBED_BATCH_SIZE", "8")))
    return np.concatenate([encoder.encode(texts[i:i + batch_size]) for i in range(0, len(texts), batch_size)])


@lru_cache(maxsize=4)
def _load_index(path_str, mtime_ns, size, dim):
    import numpy as np
    import pyarrow.parquet as pq
    table = pq.read_table(path_str, columns=["id", "text_sha", "vector"])
    ids = np.asarray(table["id"].to_pylist(), dtype=str)
    vectors = np.asarray(table["vector"].to_pylist(), dtype=np.float32).reshape(len(ids), dim)
    checked_vectors(vectors, len(ids), dim)
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate ids in embedding parquet")
    if len(ids):
        vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    return ids, vectors


def search(label: str, query_vec, k: int = 10) -> list[tuple[str, float]]:
    import numpy as np
    label = label.lower()
    if label not in ("product", "item", "style", "term"):
        raise ValueError(f"Unsupported node label: {label}")
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError("k must be a nonnegative integer")
    manifest = read_manifest()
    dim = manifest["dimension"]
    query = np.asarray(query_vec, dtype=np.float32)
    if query.shape == (1, dim):
        query = query[0]
    if query.shape != (dim,) or not np.isfinite(query).all() or np.linalg.norm(query) <= 0:
        raise ValueError(f"Query requires finite nonzero shape ({dim},)")
    filename = manifest["outputs"][label]["path"]
    path = (output_dir() / filename).resolve()
    if not path.is_relative_to(output_dir().resolve()):
        raise ValueError("Index path escapes embeddings directory")
    stat = path.stat()
    ids, vectors = _load_index(str(path), stat.st_mtime_ns, stat.st_size, dim)
    if len(ids) != manifest["outputs"][label]["rows"]:
        raise ValueError("Parquet count differs from manifest")
    if not k or not len(ids):
        return []
    scores = vectors @ (query / np.linalg.norm(query))
    order = np.lexsort((ids, -scores))[:min(k, len(ids))]
    return [(str(ids[i]), float(np.clip(scores[i], -1, 1))) for i in order]
