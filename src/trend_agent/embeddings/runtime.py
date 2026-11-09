"""로컬 임베딩 인코더 (유료 API 없음). `embed_texts()`가 공개 진입점이다. 모델 캐시는 Hugging Face 기본 위치(`HF_HOME`)를 쓴다."""
from __future__ import annotations

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


class Encoder:
    def __init__(self, model: str, *, revision: str | None = None):
        if model not in SPECS:
            raise ValueError(f"Unsupported local model: {model}")
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
    """텍스트 목록 → float32 (N, 1024), L2 정규화. 기본 모델은 `BAAI/bge-m3`다(prefix 없음).

    허용 모델: BAAI/bge-m3, (사용자 승인 대체) intfloat/multilingual-e5-large. import만으로는 모델을 읽지 않고 첫 호출에서 읽는다.
    """
    if not isinstance(texts, list):
        raise TypeError("texts must be list[str]")
    model = model or DEFAULT_MODEL
    if model not in SPECS:
        raise ValueError(f"Unsupported local model: {model}")
    import numpy as np
    if not texts:
        return np.empty((0, SPECS[model]["dim"]), dtype=np.float32)
    encoder = _query_encoder(model, None)
    batch_size = max(1, int(os.environ.get("EMBED_BATCH_SIZE", "8")))
    return np.concatenate([encoder.encode(texts[i:i + batch_size]) for i in range(0, len(texts), batch_size)])
