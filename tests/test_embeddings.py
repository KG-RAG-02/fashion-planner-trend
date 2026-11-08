"""Local fixtures only; fake encoder never downloads weights or calls a service."""
import argparse
import hashlib
import json

import numpy as np
import pyarrow.parquet as pq
import pytest

from trend_agent.embeddings import batch, runtime
from trend_agent.embeddings.text import collect_inputs, garment_text, style_text, text_sha


def test_canonical_text_omits_null_and_marks_approx():
    rec = {"category_major": "상의", "colors": [{"value": "퍼플", "approx": True}, {"value": None, "raw": "미확인"}],
           "materials": [{"value": "울"}, {"value": "울"}], "node": {"name": "  보라색\n니트 ", "fit": "타이트",
           "approx": ["fit"], "length_sleeve": "긴팔"}}
    assert garment_text(rec) == "상의 | 색 ~퍼플 | 소재 울 | 핏 ~타이트 | 기장 /긴팔 | 보라색 니트"
    assert garment_text({"node": {"name": "e\u0301"}}) == "é"
    assert style_text({"name": "스타일", "aliases": ["B", "A", "A"], "definition": "정의",
                       "description": [{"text": "설명", "evidence": [{"quote": "인용 제외"}]}]}) == "스타일 | A | B | 정의 | 설명"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    shared = tmp_path / "_shared"
    shared.mkdir()
    board = tmp_path / "_vault/00_Team/BOARD.md"
    board.parent.mkdir(parents=True)
    board.write_text("\n".join(f"<!-- {key}:BEGIN -->\nunchanged\n<!-- {key}:END -->"
                               for key in ("CONTRACTS", "DECISIONS", "USER")))
    monkeypatch.setenv("SHARED", str(shared))
    monkeypatch.setenv("VOGUE_EMBEDDINGS_DIR", str(shared / "embeddings"))
    monkeypatch.setattr(batch, "event", lambda *a, **kw: None)
    runtime._load_index.cache_clear()
    return shared


class FakeEncoder:
    documents = []

    def __init__(self, model, revision=None):
        self.model_name, self.revision, self.dim = model, revision or "fixture-revision", 1024
        self.device, self.mps_available, self.load_seconds = "cpu", False, 0
        self.failures, self.timings, self.truncated_texts = [], {"cpu": 0.0, "mps": 0.0}, 0

    def encode(self, texts, documents=False):
        if documents:
            self.documents.extend(texts)
        vectors = np.stack([np.resize(np.frombuffer(hashlib.sha256(t.encode()).digest(), dtype=np.uint8), self.dim)
                            for t in texts]).astype(np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def write_products(shared, version, names):
    path = shared / f"data/processed/{version}/products.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"category_major": "상의", "node": {"id": str(i), "name": name}})
                              for i, name in enumerate(names)), encoding="utf-8")


def test_incremental_job_and_manifest_search(environment, monkeypatch):
    shared = environment
    monkeypatch.setattr(batch, "Encoder", FakeEncoder)
    FakeEncoder.documents = []
    args = argparse.Namespace(product_version=None, extraction_version=None, model=runtime.DEFAULT_MODEL,
                              batch_size=2, inventory_only=False)
    write_products(shared, "v1", ["니트", "니트", "셔츠"])
    batch.run(args)
    first = runtime.read_manifest()
    assert (first["outputs"]["product"]["rows"], first["unique_texts"], first["newly_encoded"]) == (3, 2, 2)
    assert first["validation"]["product"]["nan_count"] == 0
    assert runtime.search("Term", np.ones(1024), 5) == []
    vector = FakeEncoder(runtime.DEFAULT_MODEL).encode(["상의 | 니트"])[0]
    hits = runtime.search("Product", vector, 2)
    assert [node_id for node_id, _ in hits] == ["0", "1"]
    assert hits[0][1] == pytest.approx(1, abs=2e-6)
    with pytest.raises(ValueError):
        runtime.search("Product", np.ones(10))
    with pytest.raises(ValueError):
        runtime.search("Product", np.full(1024, np.nan))
    with pytest.raises(ValueError):
        runtime.search("Product", np.zeros(1024))
    batch.run(args)
    second = runtime.read_manifest()
    assert (second["cache_reused"], second["newly_encoded"]) == (2, 0)
    write_products(shared, "v2", ["니트", "니트", "재킷"])
    batch.run(args)
    third = runtime.read_manifest()
    assert third["input"]["product_version"] == "v2"
    assert (third["cache_reused"], third["newly_encoded"]) == (1, 1)
    assert len(FakeEncoder.documents) == 3
    assert pq.read_table(shared / "embeddings/text_index.parquet").num_rows == 2


def test_committed_cache_survives_interruption(environment):
    cache_path = environment / "cache.sqlite3"
    enc = FakeEncoder(runtime.DEFAULT_MODEL)
    cache = batch.VectorCache(cache_path)
    cache.configure(enc)
    vec = enc.encode(["니트"], documents=True)
    cache.put(enc.model_name, [text_sha("니트")], vec)
    cache.db.close()
    resumed = batch.VectorCache(cache_path)
    found = resumed.load(enc.model_name, [text_sha("니트"), text_sha("new")], 1024)
    assert list(found) == [text_sha("니트")]
    assert np.array_equal(found[text_sha("니트")], vec[0])
    enc.revision = "different-revision"
    with pytest.raises(RuntimeError, match="revision/settings changed"):
        resumed.configure(enc)
    resumed.db.close()


def test_board_directive_change_stops_before_next_phase(environment):
    out = environment / "embeddings"
    out.mkdir()
    guard = batch.BoardGuard(environment, out)
    guard.path.write_text(guard.path.read_text().replace("<!-- USER:END -->", "@C1 중지\n<!-- USER:END -->"))
    with pytest.raises(RuntimeError, match="BOARD"):
        guard.check("encode")


def test_failed_primary_model_switches_whole_job_to_e5(environment, monkeypatch):
    shared = environment
    write_products(shared, "v1", ["니트"])
    def load(model, revision=None):
        if model == runtime.DEFAULT_MODEL:
            raise RuntimeError("fixture primary model unavailable")
        return FakeEncoder(model, revision)
    monkeypatch.setattr(batch, "Encoder", load)
    args = argparse.Namespace(product_version=None, extraction_version=None, model=runtime.DEFAULT_MODEL,
                              batch_size=2, inventory_only=False)
    batch.run(args)
    manifest = runtime.read_manifest()
    assert manifest["model"] == runtime.FALLBACK_MODEL
    assert manifest["model_failures"][0]["model"] == runtime.DEFAULT_MODEL
    assert manifest["outputs"]["product"]["rows"] == 1


def test_e5_prefix_and_mps_cpu_retry():
    class Stub:
        max_seq_length = 512
        device = "mps"
        calls = []

        def tokenizer(self, texts, **kwargs):
            return {"input_ids": [[1] for _ in texts]}

        def encode(self, texts, **kwargs):
            self.calls.append((self.device, texts))
            if self.device == "mps":
                raise RuntimeError("fixture MPS denied")
            return np.ones((len(texts), 1024), dtype=np.float32) / 32

        def to(self, device):
            self.device = device

    encoder = runtime.Encoder.__new__(runtime.Encoder)
    encoder.model_name, encoder.dim, encoder.device = runtime.FALLBACK_MODEL, 1024, "mps"
    encoder.encoder = Stub()
    encoder.failures, encoder.timings, encoder.truncated_texts = [], {"mps": 0.0, "cpu": 0.0}, 0
    result = encoder.encode(["니트"], documents=True)
    assert result.shape == (1, 1024)
    assert encoder.device == "cpu"
    assert encoder.encoder.calls[:2] == [("mps", ["passage: 니트"]), ("cpu", ["passage: 니트"])]
    encoder.encode(["질의"])
    assert encoder.encoder.calls[-1] == ("cpu", ["query: 질의"])
    assert encoder.failures[0]["device"] == "mps"


def test_term_map_adapter_preserves_t3_graph_ids(environment):
    write_products(environment, "v1", ["니트"])
    termmap = environment / "terms/term_map.jsonl"
    termmap.parent.mkdir()
    termmap.write_text(json.dumps({"row": "alias", "term": "고프코어 룩", "norm": "고프코어"}), encoding="utf-8")
    rows, _ = collect_inputs(environment)
    assert rows["term"][0]["id"] == "term:고프코어"
    assert rows["term"][0]["text"] == "고프코어 룩"
    with termmap.open("a") as stream:
        stream.write("\n" + json.dumps({"term": "고프코어", "norm": "고프코어"}))
    with pytest.raises(ValueError, match="Conflicting term id"):
        collect_inputs(environment)


def test_keyword_inventory_uses_explicit_norm_and_marks_synthetic_ids(environment):
    write_products(environment, "v1", ["니트"])
    inventory = environment / "keywords/inventory.csv"
    inventory.parent.mkdir()
    inventory.write_text("keyword,norm\n고프코어 룩,고프코어\n시럽코어,\n시럽코어,\n", encoding="utf-8")
    rows, metadata = collect_inputs(environment)
    ids = {row["id"] for row in rows["term"]}
    assert ids == {"term:고프코어", "keyword:" + text_sha("시럽코어")}
    assert metadata["statistics"]["keyword_synthetic_ids"] == 1
