"""데이터 계약(그래프 노드·결과 스키마·가중치 설정) 테스트. 라벨 파일(gitignore)은 쓰지 않고 작은 가짜 데이터만 쓴다."""
import importlib
import sys

import pytest
import yaml
from pydantic import ValidationError

from fashion_planner_trend.retrieval.weights import DEFAULT_PATH, load_weights
from fashion_planner_trend.schemas.article_extraction import ExtractedStyle
from fashion_planner_trend.schemas.graph_schema import (
    ProductNode,
    flatten,
    normalize_term,
    style_node_from_extraction,
)
from fashion_planner_trend.schemas.trend_analysis_result import (
    SCHEMA_VERSION,
    TrendAnalysisResult,
    evidence_id,
)

BASE = {"source": "otpensource", "kind": "catalog_item", "source_row": "r"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("‘절제된 맥시멀리즘’", "절제된맥시멀리즘"),
        ("발레코어 룩", "발레코어룩"),            # 끝의 일반어는 지우지 않는다
        ("Quiet Luxury", "quietluxury"),
        ("Quiet Luxury Style", "quietluxurystyle"),
        ("Y2K 룩", "y2k룩"),
        ("조용한 럭셔리", "조용한럭셔리"),
        ("뉴 로 (New Raw)", "뉴로newraw"),       # 공백·괄호·대소문자만 정리한다
        ("룩", "룩"),
    ],
)
def test_normalize_term_examples(raw, expected):
    assert normalize_term(raw) == expected


def test_normalize_term_keeps_generic_suffix():
    assert normalize_term("코어 룩 스타일") == "코어룩스타일"


def test_product_without_optional_fields_flattens_to_required_only():
    p = ProductNode(id="otpensource:a:1", **BASE)
    assert set(flatten(p)) == {"id", "source", "kind", "source_row", "sub_colors", "approx"}
    assert ProductNode.model_validate({"id": "x", **BASE}).snap_id is None  # 선택 필드는 없어도 유효


def test_product_image_fields_and_has_image():
    snap = ProductNode(id="musinsa_snap:7:상의", source="musinsa_snap", kind="snap_garment", source_row="r",
                       snap_id=7, image_ref="hf://datasets/x@rev#row=7", image_scope="full_snap",
                       image_status="ok", multi_wearer_suspected=True)
    flat = flatten(snap)
    assert snap.has_image and flat["snap_id"] == 7 and flat["image_scope"] == "full_snap"
    assert "dup_of" not in flat and "multipack" not in flat  # None은 속성을 만들지 않는다
    assert not ProductNode(id="b", image_url="https://x", image_status="broken", **BASE).has_image
    assert ProductNode(id="c", image_url="https://x", **BASE).has_image          # 상태 없음 → 참조 존재
    assert not ProductNode(id="d", **BASE).has_image
    with pytest.raises(ValidationError):  # HEAD 원시 상태는 계약 값이 아니다 (사용자 결정)
        ProductNode(id="e", image_status="head_http_404", **BASE)
    with pytest.raises(ValidationError):
        ProductNode(id="f", image_scope="crop", **BASE)
    part = ProductNode(id="otpensource:a:1#2", parent_id="otpensource:a:1", split_from="name_rule",
                       image_scope="full_set", **BASE)
    assert flatten(part)["parent_id"] == "otpensource:a:1"


def test_split_from_review():
    """사람이 채택한 검수 분해는 split_from="review". 정의 밖 값은 거부한다."""
    part = ProductNode(id="otpensource:a:2#1", parent_id="otpensource:a:2", split_from="review",
                       image_scope="full_set", **BASE)
    assert flatten(part)["split_from"] == "review"
    with pytest.raises(ValidationError):
        ProductNode(id="otpensource:a:3#1", parent_id="otpensource:a:3", split_from="vision", **BASE)


def test_style_node_norm_aliases_article_id():
    s = ExtractedStyle(name="피스타치오 룩", name_origin="article", definition="d", aliases=["Pistachio Look", "피스타치오 룩", "Pistachio Look"])
    n = style_node_from_extraction(s, "900001-s0")
    assert n.article_id == "900001" and n.name_norm == "피스타치오룩"
    assert n.aliases == ["Pistachio Look", "피스타치오 룩"]          # 순서 보존 중복 제거
    assert n.aliases_norm == ["pistachiolook"]                # name_norm과 같은 값은 뺀다
    assert ExtractedStyle(name="x", name_origin="article", definition="d").aliases == []
    assert style_node_from_extraction(s, "s1").article_id is None


def test_evidence_id_is_stable_and_formatted():
    a = evidence_id("900001", 8, "반복되는 체크 패턴")
    assert a == evidence_id("900001", 8, "반복되는 체크 패턴")
    assert a.startswith("trend:900001:8:") and len(a.rsplit(":", 1)[1]) == 8
    assert a != evidence_id("900001", 8, "반복되는 체크 패턴이")


def test_result_defaults_and_closed_status():
    r = TrendAnalysisResult.model_validate({"request_id": "r", "status": "ok", "trends": [], "evidence": [], "limitations": []})
    assert r.schema_version == SCHEMA_VERSION == "1.0.0"
    for status in ("ok", "no_results", "error"):
        assert TrendAnalysisResult(request_id="r", status=status).status == status
    with pytest.raises(ValidationError):  # partial은 이 계약에 없다
        TrendAnalysisResult(request_id="r", status="partial")


def test_weights_yaml_has_only_scoring_sections():
    d = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    assert set(d) == {"base_weights", "category", "focus_multipliers"}
    assert load_weights().base_weights["color"] == 3


def test_llm_contract_imports_lazily():
    sys.modules.pop("fashion_planner_trend.llm", None)
    llm = importlib.import_module("fashion_planner_trend.llm")
    assert {"structured", "call", "structured_model", "strict_schema", "schema_sha256", "LLMResult",
            "LLMError", "LLMBackendError", "LLMOutputError", "LLMTimeout"} <= set(llm.__all__)
    with pytest.raises(AttributeError):
        getattr(llm, "not_a_contract_name")
