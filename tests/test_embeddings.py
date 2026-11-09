"""임베딩 진입점: 가짜 인코더만 쓴다 (모델을 내려받거나 torch를 읽지 않는다)."""
import pytest

np = pytest.importorskip("numpy")  # 데모 이미지에는 numpy가 없어 이 파일은 건너뛴다

from trend_agent.embeddings import runtime


class _FakeEncoder:
    def __init__(self):
        self.calls = []

    def encode(self, texts, *, documents=False):
        self.calls.append(list(texts))
        out = np.zeros((len(texts), runtime.SPECS[runtime.DEFAULT_MODEL]["dim"]), dtype=np.float32)
        out[:, 0] = 1.0
        return out


@pytest.fixture
def fake(monkeypatch):
    enc = _FakeEncoder()
    monkeypatch.setattr(runtime, "_query_encoder", lambda model, revision: enc)
    return enc


def test_default_model_is_bge_m3():
    assert runtime.DEFAULT_MODEL == "BAAI/bge-m3" and runtime.SPECS[runtime.DEFAULT_MODEL]["dim"] == 1024


def test_embed_texts_empty_has_shape_without_loading_model():
    assert runtime.embed_texts([]).shape == (0, 1024)


def test_embed_texts_batches_and_returns_float32(fake, monkeypatch):
    monkeypatch.setenv("EMBED_BATCH_SIZE", "2")
    out = runtime.embed_texts(["a", "b", "c"])
    assert out.shape == (3, 1024) and out.dtype == np.float32
    assert [len(c) for c in fake.calls] == [2, 1]


def test_embed_texts_rejects_bad_input():
    with pytest.raises(TypeError):
        runtime.embed_texts("not a list")
    with pytest.raises(ValueError):
        runtime.embed_texts(["a"], model="some/other-model")


def test_checked_vectors_rejects_zero_and_nonfinite():
    ok = np.ones((2, 4), dtype=np.float32)
    assert runtime.checked_vectors(ok, 2, 4).shape == (2, 4)
    with pytest.raises(ValueError):
        runtime.checked_vectors(np.zeros((2, 4)), 2, 4)
    with pytest.raises(ValueError):
        runtime.checked_vectors(np.full((2, 4), np.nan), 2, 4)
    with pytest.raises(ValueError):
        runtime.checked_vectors(ok, 3, 4)
