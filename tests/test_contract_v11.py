"""계약 v1.1 (G1) 회귀·호환 테스트. 라벨 파일(gitignore)은 쓰지 않고 작은 가짜 데이터만 쓴다."""
import importlib
import sys

import pytest
import yaml
from pydantic import ValidationError

from trend_agent.retrieval.weights import DEFAULT_PATH, load_weights
from trend_agent.schemas.article_extraction import ExtractedStyle
from trend_agent.schemas.graph_schema import (
    AliasOfEdge,
    MapsToEdge,
    ProductNode,
    TermNode,
    flatten,
    normalize_term,
    style_node_from_extraction,
    term_id,
)
from trend_agent.schemas.trend_analysis_result import (
    QUALIFICATION_BY_MODE,
    SCHEMA_VERSION,
    TrendAnalysisResult,
    TrendPick,
    evidence_id,
    virtual_item_id,
    virtual_style_id,
)

BASE = {"source": "otpensource", "kind": "catalog_item", "source_row": "r"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("‘절제된 맥시멀리즘’", "절제된맥시멀리즘"),
        ("발레코어 룩", "발레코어"),
        ("Quiet Luxury Style", "quietluxury"),
        ("Y2K 룩", "y2k"),
        ("조용한 럭셔리", "조용한럭셔리"),
        ("드뮤어 트렌드", "드뮤어"),
        ("룩", "룩"),          # 남는 길이 < 2 이면 지우지 않는다
        ("스타일", "스타일"),
    ],
)
def test_normalize_term_examples(raw, expected):
    assert normalize_term(raw) == expected


def test_normalize_term_removes_one_suffix_only():
    assert normalize_term("코어 룩 스타일") == "코어룩"


def test_term_node_keys_must_be_consistent():
    t = TermNode(id=term_id("발레코어"), text="발레코어 룩", norm="발레코어", kind="trend_word", origin="manual")
    assert t.id == "term:발레코어"
    with pytest.raises(ValidationError):  # id가 norm과 맞지 않음
        TermNode(id="term:x", text="발레코어", norm="발레코어", kind="alias", origin="manual")
    with pytest.raises(ValidationError):  # norm이 normalize_term(text)와 맞지 않음
        TermNode(id="term:발레", text="발레코어", norm="발레", kind="alias", origin="manual")
    assert AliasOfEdge(origin="label", article_id="a").origin == "label"
    assert MapsToEdge(attribute="color", value="퍼플", level="core", value_level="primary").origin == "llm"


def test_v10_product_flattens_exactly_as_before():
    p = ProductNode(id="otpensource:a:1", **BASE)
    assert set(flatten(p)) == {"id", "source", "kind", "source_row", "sub_colors", "approx"}
    assert ProductNode.model_validate({"id": "x", **BASE}).snap_id is None  # v1.0 JSON 그대로 유효


def test_v11_product_fields_and_has_image():
    snap = ProductNode(id="musinsa_snap:7:상의", source="musinsa_snap", kind="snap_garment", source_row="r",
                       snap_id=7, image_ref="hf://datasets/x@rev#row=7", image_scope="full_snap",
                       image_status="ok", multi_wearer_suspected=True)
    flat = flatten(snap)
    assert snap.has_image and flat["snap_id"] == 7 and flat["image_scope"] == "full_snap"
    assert "dup_of" not in flat and "multipack" not in flat  # None은 속성을 만들지 않는다
    assert not ProductNode(id="b", image_url="https://x", image_status="broken", **BASE).has_image
    assert ProductNode(id="c", image_url="https://x", **BASE).has_image          # 상태 없음 → 참조 존재
    assert not ProductNode(id="d", **BASE).has_image
    with pytest.raises(ValidationError):  # HEAD 원시 상태는 계약 값이 아니다 (결정 G0-C2)
        ProductNode(id="e", image_status="head_http_404", **BASE)
    with pytest.raises(ValidationError):
        ProductNode(id="f", image_scope="crop", **BASE)
    part = ProductNode(id="otpensource:a:1#2", parent_id="otpensource:a:1", split_from="name_rule",
                       image_scope="full_set", **BASE)
    assert flatten(part)["parent_id"] == "otpensource:a:1"


def test_v13_split_from_review():
    """계약 v1.3: 사용자가 채택한 검수 분해는 split_from="review". 정의 밖 값은 거부한다."""
    part = ProductNode(id="otpensource:a:2#1", parent_id="otpensource:a:2", split_from="review",
                       image_scope="full_set", **BASE)
    assert flatten(part)["split_from"] == "review"
    with pytest.raises(ValidationError):
        ProductNode(id="otpensource:a:3#1", parent_id="otpensource:a:3", split_from="vision", **BASE)


def test_style_node_v11_norm_aliases_article_id():
    s = ExtractedStyle(name="뉴 로", name_origin="article", definition="d", aliases=["New Raw", "뉴 로", "New Raw"])
    n = style_node_from_extraction(s, "881396-s0")
    assert n.article_id == "881396" and n.name_norm == "뉴로"
    assert n.aliases == ["New Raw", "뉴 로"]          # 순서 보존 중복 제거
    assert n.aliases_norm == ["newraw"]                # name_norm과 같은 값은 뺀다
    assert ExtractedStyle(name="x", name_origin="article", definition="d").aliases == []
    assert style_node_from_extraction(s, "s1").article_id is None


def test_evidence_id_is_stable_and_formatted():
    a = evidence_id("881396", 8, "오묘한 패턴")
    assert a == evidence_id("881396", 8, "오묘한 패턴")
    assert a.startswith("trend:881396:8:") and len(a.rsplit(":", 1)[1]) == 8
    assert a != evidence_id("881396", 8, "오묘한 패턴이")


def test_virtual_ids():
    assert virtual_style_id(4, "드뮤어") == "virtual:4:드뮤어"
    assert virtual_item_id(4, "드뮤어", 0) == "virtual:4:드뮤어-i0"


def test_result_v10_json_parses_and_partial_is_allowed():
    r = TrendAnalysisResult.model_validate({"request_id": "r", "status": "ok", "trends": [], "evidence": [], "limitations": []})
    assert r.schema_version == SCHEMA_VERSION == "1.1" and r.resolutions == [] and r.applied_settings is None
    assert TrendAnalysisResult(request_id="r", status="partial").status == "partial"
    with pytest.raises(ValidationError):
        TrendAnalysisResult(request_id="r", status="maybe")


def test_qualification_by_mode_and_trend_pick():
    assert QUALIFICATION_BY_MODE == {"graph": "article_evidence", "semantic": "article_evidence",
                                     "attribute": "vocabulary_rule", "llm_inferred": "agent_proposal"}
    tp = TrendPick(product_id="p", source="otpensource", kind="catalog_item", rank=1, trend_score=0.8,
                   item_id="i", item_name="n", item_focus=1.0)
    assert tp.has_image is False and tp.also_from_items == [] and tp.gender is None


def test_weights_v11_structure_and_v10_loader():
    d = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    flags = d["flags"]
    assert flags["season_policy"] in ("strict", "report")
    assert (flags["image_first_tiebreak"], flags["core_unknown_decay"], flags["snap_id_limit"],
            flags["item_gender_filter"]) == (True, 0.5, 1, True)            # v1.4: T4 평가 권장값(키가 없을 때 코드 기본값은 v1.0 동작 — test_scoring_v11)
    assert d["tier4"]["value_weights"] == {"primary": 1.0, "secondary": 0.5}
    assert d["resolver"]["tier3"] == {"tau": None, "delta": None}
    assert load_weights().base_weights["color"] == 3                         # v1.0 로더는 새 키를 무시한다


def test_llm_and_embeddings_contracts_import_lazily():
    torch_before = "torch" in sys.modules
    for name in ("trend_agent.llm", "trend_agent.embeddings"):
        sys.modules.pop(name, None)
    llm = importlib.import_module("trend_agent.llm")
    emb = importlib.import_module("trend_agent.embeddings")
    assert {"structured", "call", "structured_model", "strict_schema", "schema_sha256", "LLMResult",
            "LLMError", "LLMBackendError", "LLMOutputError", "LLMTimeout"} <= set(llm.__all__)
    assert emb.__all__ == ["embed_texts", "search"]
    assert ("torch" in sys.modules) == torch_before  # import만으로 torch를 읽지 않는다
    with pytest.raises(AttributeError):
        getattr(llm, "not_a_contract_name")
