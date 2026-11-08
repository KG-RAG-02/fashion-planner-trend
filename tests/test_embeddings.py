"""Local fixtures only; fake encoder never downloads weights or calls a service."""
import argparse
import hashlib
import json

import numpy as np
import pyarrow.parquet as pq
import pytest

from trend_agent.embeddings import batch, runtime
from trend_agent.embeddings.text import (PreprocessAudit, clean, collect_inputs, garment_text,
                                        input_plan, product_name, style_text, text_sha)


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
    if version == "v2":
        handoffs = shared.parent / "_vault/00_Team/handoffs"
        handoffs.mkdir(parents=True, exist_ok=True)
        (handoffs / "T2-T3-products-v2.md").write_text("products-v2 ready")


def test_incremental_job_and_manifest_search(environment, monkeypatch):
    shared = environment
    monkeypatch.setattr(batch, "Encoder", FakeEncoder)
    FakeEncoder.documents = []
    args = argparse.Namespace(product_version="v1", extraction_version=None, model=runtime.DEFAULT_MODEL,
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
    args.product_version = None
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


def test_v2_model_failure_does_not_change_phase1_model(environment, monkeypatch):
    shared = environment
    write_products(shared, "v2", ["니트"])
    attempted = []
    def load(model, revision=None):
        attempted.append((model, revision))
        raise RuntimeError("fixture primary model unavailable")
    monkeypatch.setattr(batch, "Encoder", load)
    args = argparse.Namespace(product_version=None, extraction_version=None, model=runtime.DEFAULT_MODEL,
                              batch_size=2, inventory_only=False)
    with pytest.raises(RuntimeError, match="All local model attempts failed"):
        batch.run(args)
    assert attempted == [(runtime.DEFAULT_MODEL, batch.MODEL_REVISION)]
    assert not (shared / "embeddings/manifest.json").exists()


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
    rows, _ = collect_inputs(environment)
    assert rows["term"][0]["text"] == "고프코어 룩 | 고프코어"
    with termmap.open("a") as stream:
        stream.write("\n" + json.dumps({"term": "다른 말", "norm": "고프코어"}))
    with pytest.raises(ValueError, match="contract normalization"):
        collect_inputs(environment)


def test_keyword_inventory_is_protection_only(environment):
    write_products(environment, "v1", ["니트"])
    inventory = environment / "keywords/inventory.csv"
    inventory.parent.mkdir()
    inventory.write_text("keyword,norm\n고프코어 룩,고프코어\n시럽코어,\n시럽코어,\n", encoding="utf-8")
    rows, metadata = collect_inputs(environment)
    ids = {row["id"] for row in rows["term"]}
    assert ids == set()
    assert metadata["preprocessing"]["protected_count"] >= 2
    assert "term_map" in metadata["pending"]


@pytest.mark.parametrize("raw,expected", [
    (" ＦＲＥＥ  와이드\t팬츠 &#039;빈티지&#039; ✨🔥", "와이드 팬츠 '빈티지'"),
    ("[무료배송][단독] 오버핏 크롭 니트 (당일발송)", "오버핏 크롭 니트"),
    ("[단독 와이드] 팬츠", "와이드 팬츠"),
    ("[30% 적립금] 빈티지 니트", "빈티지 니트"),
    ("[SET] 셋업 투피스 (빈티지)", "[SET] 셋업 투피스 (빈티지)"),
    ("니트 FREE S/M/L/XL 사이즈: 90/95/100", "니트"),
    ("오버사이즈 빅사이즈 셔츠 S~4XL", "오버사이즈 빅사이즈 셔츠"),
    ("오버 사이즈 헤비 셔츠 사이즈 M", "오버 사이즈 헤비 셔츠"),
    ("오버 사이즈핏 빈티지 니트", "오버 사이즈핏 빈티지 니트"),
    ("L/S Tee S/S Tee", "L/S Tee S/S Tee"),
    ("P.A.M. Hoodie F.S.B (다크 M.그레이)", "P.A.M. Hoodie F.S.B (다크 M.그레이)"),
    ("THINSULATE™ 니트<br>와이드 팬츠", "THINSULATE 니트 와이드 팬츠"),
    ("KLTW0153 빈티지 티셔츠 Q321-3152-1MG", "빈티지 티셔츠"),
    ("[5597] 크롭 니트", "크롭 니트"),
    ("Polo_CWTAW24992BRX DF42ST11_WHITE", "Polo WHITE"),
    ("BODYSUIT_T316BS300 A13HD571BLACK", "BODYSUIT BLACK"),
    ("1980년대 빈티지 1000g 니트 MA-1", "1980년대 빈티지 1000g 니트 MA-1"),
    ("니트 블랙 BK BK", "니트 블랙"),
    ("BLACK BK 블루 BL", "BLACK 블루"),
    ("니트 다크그레이 라이트블루", "니트 다크그레이 라이트블루"),
    ("1️⃣★ 와이드 👩‍🎤 팬츠", "와이드 팬츠"),
])
def test_v2_product_name_rules(raw, expected):
    assert product_name(raw) == expected


def test_common_rules_and_audit_counters():
    audit = PreprocessAudit()
    assert clean(" Ａ&#039;\n니트✨", audit) == "A' 니트"
    assert all(audit.counts[k] == 1 for k in ("unicode_nfkc", "html_unescape", "symbols_removed", "whitespace"))
    product_name("[단독] 니트 FREE KLTW1234 블랙 BK", audit=audit)
    assert all(audit.counts[k] == 1 for k in ("promotion", "size_option", "product_code", "duplicate_color"))


def test_inventory_protects_digit_keyword_and_bracket_meaning():
    assert product_name("[단독] 2026FW 크롭 ABC12345", protected=["2026FW"]) == "2026FW 크롭"


def test_component_text_uses_category_without_source_mutation():
    source = {"category_major": "하의", "colors": [{"value": "퍼플", "approx": True}],
              "node": {"name": "[무료배송] 투피스 셋업", "category_minor": "팬츠", "fit": "타이트", "approx": ["fit"],
                       "parent_id": "private-parent", "split_from": "name_rule", "image_ref": "secret-image", "image_status": "ok"}}
    before = json.dumps(source)
    assert garment_text(source) == "하의>팬츠 | 색 ~퍼플 | 핏 ~타이트 | 투피스 셋업 | 세트 구성품 팬츠"
    assert json.dumps(source) == before
    assert "private-parent" not in garment_text(source)
    assert "secret-image" not in garment_text(source)


@pytest.mark.parametrize("mask", range(8))
def test_readiness_independent_inputs(environment, mask):
    shared = environment
    write_products(shared, "v2", ["니트"])
    handoffs = shared.parent / "_vault/00_Team/handoffs"
    if not mask & 1:
        (handoffs / "T2-T3-products-v2.md").unlink()
    ext = shared / "extractions/extract-v2/1.json"
    ext.parent.mkdir(parents=True)
    ext.write_text(json.dumps({"extraction": {"article_id": "1", "styles": [{"name": "스타일", "items": []}]},
                               "label_meta": {"prompt_version": "extract-v2"}}))
    if mask & 2:
        (handoffs / "T1-T3,T4-extractions-ready.md").write_text("version: extract-v2")
    if mask & 4:
        termmap = shared / "terms/term_map.jsonl"
        termmap.parent.mkdir()
        termmap.write_text(json.dumps({"text": "드뮤어", "aliases": ["demure"]}))
    rows, metadata = collect_inputs(shared)
    assert (len(rows["product"]), len(rows["style"]), len(rows["term"])) == tuple(bool(mask & n) for n in (1, 2, 4))
    assert len(metadata["pending"]) == 3 - mask.bit_count()
    if mask & 4:
        assert rows["term"][0]["text"] == "드뮤어 | demure"


def test_product_sidecar_excluded_and_removed_ids_not_published(environment, monkeypatch):
    monkeypatch.setattr(batch, "Encoder", FakeEncoder)
    shared = environment
    write_products(shared, "v2", ["[단독] 니트", "니트", "셔츠"])
    sidecar = shared / "data/processed/v2/otpensource_split_parents.jsonl"
    sidecar.write_text('not a ProductRecord')
    args = argparse.Namespace(product_version=None, extraction_version=None, model=runtime.DEFAULT_MODEL,
                              batch_size=2, inventory_only=False)
    batch.run(args)
    first = runtime.read_manifest()
    write_products(shared, "v2", ["니트"])
    args.baseline_run = first["run_id"]
    batch.run(args)
    current = runtime.read_manifest()
    assert current["newly_encoded"] == 0
    assert current["outputs"]["product"]["rows"] == 1
    comparison = current["probe_comparison"]
    assert comparison["status"] == "complete"
    assert comparison["id_changes"]["product"]["removed"] == 2
    assert pq.read_table(shared / "embeddings" / first["outputs"]["product"]["path"]).num_rows == 3


def test_no_inputs_never_loads_model(environment, monkeypatch):
    monkeypatch.setattr(batch, "Encoder", lambda *a, **k: pytest.fail("model loaded for pending input"))
    args = argparse.Namespace(product_version=None, extraction_version=None, model=runtime.DEFAULT_MODEL,
                              batch_size=2, inventory_only=False)
    batch.run(args)
    pending = json.loads((environment / "embeddings/pending_manifest.json").read_text())
    assert pending["status"] == "pending"
    assert len(pending["pending"]) == 3
    assert pending["newly_encoded"] == 0


def test_file_added_after_inventory_stops_export(environment):
    write_products(environment, "v2", ["니트"])
    _, metadata = collect_inputs(environment)
    termmap = environment / "terms/term_map.jsonl"
    termmap.parent.mkdir()
    termmap.write_text(json.dumps({"term": "드뮤어"}))
    args = argparse.Namespace(product_version=None, extraction_version=None)
    with pytest.raises(RuntimeError, match="Input/readiness changed"):
        batch.check_inputs(environment, args, metadata, "export")


def test_term_only_batch_and_unrecognized_profile_exclusion(environment, monkeypatch):
    monkeypatch.setattr(batch, "Encoder", FakeEncoder)
    termmap = environment / "terms/term_map.jsonl"
    termmap.parent.mkdir()
    termmap.write_text("\n".join(json.dumps(row) for row in (
        {"term": "드뮤어", "norm": "드뮤어", "aliases": ["demure"]},
        {"row": "profile", "term": "없는말", "profile": {"recognized": False}},
    )))
    args = argparse.Namespace(product_version=None, extraction_version=None, model=runtime.DEFAULT_MODEL,
                              batch_size=2, inventory_only=False)
    batch.run(args)
    manifest = runtime.read_manifest()
    assert manifest["outputs"]["product"]["rows"] == 0
    assert manifest["outputs"]["term"]["rows"] == 1
    assert set(manifest["pending"]) == {"products_v2", "extractions"}


def test_complete_run_is_verified_without_loading_model(environment, monkeypatch):
    monkeypatch.setattr(batch, "Encoder", FakeEncoder)
    write_products(environment, "v2", ["[무료배송] 와이드 니트"])
    args = argparse.Namespace(product_version=None, extraction_version=None, model=runtime.DEFAULT_MODEL,
                              batch_size=2, inventory_only=False)
    batch.run(args)
    out = environment / "embeddings"
    manifest_bytes = (out / "manifest.json").read_bytes()
    (out / "completion_state.json").write_text(json.dumps({"status": "complete", "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}))
    monkeypatch.setattr(batch, "Encoder", lambda *a, **k: pytest.fail("completed run loaded model"))
    batch.run(args)
    assert (out / "manifest.json").read_bytes() == manifest_bytes


def test_thirty_samples_source_preserved(environment):
    write_products(environment, "v2", [f"[단독] 와이드 니트 FREE {n}" for n in range(35)])
    path = environment / "data/processed/v2/products.jsonl"
    original = path.read_bytes()
    rows, metadata = collect_inputs(environment)
    assert len(rows["product"]) == 35
    assert len(metadata["preprocessing"]["samples"]) == 30
    assert metadata["preprocessing"]["semantic_losses"] == []
    assert metadata["preprocessing"]["affected_products"]["promotion"] == 35
    assert path.read_bytes() == original
