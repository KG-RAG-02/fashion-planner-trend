"""기사 추출(프롬프트·스키마·후처리·검증·재시도·출력 형식) — 합성 기사만, 실제 LLM 호출 없음."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from trend_agent.ingestion.extraction import extractor
from trend_agent.ingestion.extraction.llm_schema import extraction_schema
from trend_agent.ingestion.extraction.postprocess import postprocess
from trend_agent.ingestion.extraction.prompt import load_template, render_blocks, render_prompt, render_retry_suffix
from trend_agent.ingestion.extraction.validate import squash, validate_extraction
from trend_agent.llm import client
from trend_agent.llm.backends import BackendReply
from trend_agent.llm.schema import check_instance
from trend_agent.schemas.article_extraction import ArticleExtraction

ROOT = Path(__file__).resolve().parents[1]

# 합성 기사: 블록 0 이미지, 1 문단, 2 이미지, 3 소제목, 4 문단, 5 이미지, 6 문단, 7 문단
ARTICLE = {
    "article_id": "t-001",
    "url": "https://example.test/a/1",
    "title": "올봄 유행할 ‘테스트코어’(Test Core)",
    "published_at": "2026-10-21T01:02:03+00:00",
    "tags": ["트렌드"],
    "season": {"value": "2027SS", "source": "tag"},
    "blocks": [
        {"type": "image", "idx": 0, "image_idx": 0, "run_idx": 0, "layout": "single", "caption": "Brand A 2027 S/S RTW",
         "caption_parsed": {"type": "collection", "brand": "Brand A", "season": "2027SS"}},
        {"type": "paragraph", "idx": 1, "text": "이번 시즌 테스트코어가 떠오르고 있습니다. 화려한 컬러가 핵심이죠."},
        {"type": "image", "idx": 2, "image_idx": 1, "run_idx": 1, "layout": "two_column", "caption": "Photo: Someone",
         "caption_parsed": {"type": "credit_only"}},
        {"type": "heading", "idx": 3, "text": "버건디 셔츠"},
        {"type": "paragraph", "idx": 4, "text": "버건디 셔츠는 슬림하게 입는다. 줄무늬 바지와도 잘 맞는다."},
        {"type": "image", "idx": 5, "image_idx": 2, "run_idx": 2, "layout": "single", "caption": None,
         "caption_parsed": {"type": "none"}},
        {"type": "paragraph", "idx": 6, "text": "중간 문단입니다."},
        {"type": "paragraph", "idx": 7, "text": "멀리 떨어진 문단에 미디 스커트가 나온다."},
    ],
    "images": [{"image_idx": 0, "block_idx": 0}, {"image_idx": 1, "block_idx": 2}, {"image_idx": 2, "block_idx": 5}],
}


def _item(**kw):
    base = {"name": "버건디 셔츠", "raw_text": "버건디 셔츠는 슬림하게 입는다.", "gender": None, "category_raw": "셔츠",
            "category_major": None, "category_minor": "셔츠", "colors": [{"raw": "버건디", "value": "레드"}],
            "materials": [], "patterns": [], "details": [], "styling_raw": None, "styling_accessory": None,
            "fit_raw": "슬림", "fit": "타이트", "shape_raw": None, "shape": None, "length_body_raw": None,
            "length_body": None, "length_sleeve_raw": None, "length_sleeve": None,
            "evidence": [{"block_idx": 3, "quote": "버건디 셔츠"}, {"block_idx": 4, "quote": "슬림하게 입는다"}],
            "image_links": [{"image_block_idx": 5, "basis": "adjacent"}]}
    base.update(kw)
    return base


def _llm_doc(items=None, **style_kw):
    style = {"name": "테스트코어", "name_origin": "article", "aliases": ["Test Core"],
             "seasons": [{"year": 2027, "season": "SS"}], "definition": "합성 트렌드",
             "description": [{"text": "화려한 색", "evidence": [{"block_idx": 1, "quote": "화려한 컬러가 핵심"}]}],
             "focus_attributes": [{"attribute": "color", "level": "core",
                                   "evidence": [{"block_idx": 1, "quote": "화려한 컬러"}]}],
             "evidence": [{"block_idx": 1, "quote": "테스트코어가 떠오르고"}],
             "items": items if items is not None else [_item()]}
    style.update(style_kw)
    return {"styles": [style]}


def _validated(doc):
    pdoc, v6 = postprocess(doc, ARTICLE)
    ext = ArticleExtraction.model_validate(pdoc)
    return ext, validate_extraction(ext, ARTICLE, extra_errors=v6)


# ---------- 프롬프트 ----------
def test_template_front_matter_and_version():
    body, front = load_template("extract-v1")
    assert front["prompt_version"] == "extract-v1" and front["guideline_version"] == "0.3-draft"
    assert not body.startswith("---") and "도구를 쓰지 않는다" in body
    with pytest.raises(ValueError):
        load_template("v1")


def test_render_blocks_lines():
    text = render_blocks(ARTICLE["blocks"])
    lines = text.splitlines()
    assert lines[0] == '[0] 이미지 (run 0 · single): 캡션 "Brand A 2027 S/S RTW" → 브랜드 Brand A · 시즌 2027SS'
    assert lines[1].startswith("[1] 문단: 이번 시즌")
    assert lines[2] == "[2] 이미지 (run 1 · two_column): 캡션: 사진 출처만"
    assert lines[3] == "[3] 소제목: 버건디 셔츠"
    assert lines[5] == "[5] 이미지 (run 2 · single): 캡션 없음"
    assert "http" not in text  # 이미지 URL은 보내지 않는다


def test_render_prompt_and_retry_suffix():
    p = render_prompt(ARTICLE, "extract-v1")
    assert "- article_id: t-001" in p and "- 발행일: 2026-10-21" in p and "기사 시즌 태그: 2027SS" in p
    assert p.rstrip().endswith("JSON 객체 하나만 출력하라.")
    suf = render_retry_suffix([f"V2 e{i}" for i in range(35)], limit=30)
    assert "- V2 e29" in suf and "- V2 e30" not in suf and "외 5건" in suf


# ---------- LLM 스키마 ----------
def test_extraction_schema_variants():
    schema, dropped, sha = extraction_schema()
    assert len(sha) == 64 and len(dropped) == 2 and "article_id" not in schema["properties"]
    item = schema["properties"]["styles"]["items"]["properties"]["items"]["items"]
    assert set(item["properties"]["image_links"]["items"]["properties"]) == {"image_block_idx", "basis"}
    variants = item["properties"]["details"]["items"]["anyOf"]
    assert sorted(v["properties"]["type"]["enum"][0] for v in variants) == ["collar", "detail", "neckline"]
    ok = _llm_doc(items=[_item(details=[{"raw": "꽃 자수", "type": "detail", "value": "자수", "motif": "꽃", "placement": None}])])
    assert check_instance(ok, schema) == []
    bad = _llm_doc(items=[_item(details=[{"raw": "x", "type": "neckline", "value": "자수", "motif": None, "placement": None}])])
    assert check_instance(bad, schema)  # 넥라인 표에 '자수'는 없다


# ---------- 후처리 ----------
def test_postprocess_images_and_alias_map():
    doc = _llm_doc()
    pdoc, v6 = postprocess(doc, ARTICLE)
    assert doc["styles"][0]["items"][0]["colors"][0]["value"] == "레드"  # 원본은 그대로
    it = pdoc["styles"][0]["items"][0]
    assert pdoc["article_id"] == "t-001" and v6 == []
    assert it["image_links"] == [{"image_idx": 2, "basis": "adjacent"}]  # block 5 → 크롤러 image_idx 2
    assert it["colors"][0]["value"] == "와인" and "alias_override:colors:버건디:레드->와인" in it["review_flags"]
    assert "approx:fit:슬림->타이트" in it["review_flags"]
    ext = ArticleExtraction.model_validate(pdoc)
    assert ext.styles[0].items[0].category_major.value == "상의"


def test_postprocess_bad_image_block_is_v6_error():
    doc = _llm_doc(items=[_item(image_links=[{"image_block_idx": 4, "basis": "order"}])])
    pdoc, v6 = postprocess(doc, ARTICLE)
    assert pdoc["styles"][0]["items"][0]["image_links"] == [] and v6 and v6[0].startswith("V6")
    _ext, rep = _validated(doc)
    assert not rep.passed and rep.counts["V6"] == 1


def test_postprocess_alias_hint_flag():
    doc = _llm_doc(items=[_item(materials=[{"raw": "다운", "value": None}])])
    pdoc, _ = postprocess(doc, ARTICLE)
    assert any(f.startswith("alias_hint:materials:다운->") for f in pdoc["styles"][0]["items"][0]["review_flags"])


# ---------- 검증 V1~V6 ----------
def test_valid_doc_passes():
    _ext, rep = _validated(_llm_doc())
    assert rep.passed, rep.errors


@pytest.mark.parametrize("mutate, stage", [
    (lambda d: d["styles"][0].update(name_origin="analyst"), "V1"),
    (lambda d: d["styles"][0].update(aliases=["Made Up Name"]), "V1"),
    (lambda d: d["styles"][0]["evidence"].__setitem__(0, {"block_idx": 1, "quote": "기사에 없는 문장"}), "V2"),
    (lambda d: d["styles"][0]["evidence"].__setitem__(0, {"block_idx": 0, "quote": "Brand A"}), "V2"),
    (lambda d: d["styles"][0]["items"][0].update(fit_raw="오버핏"), "V3"),
    (lambda d: d["styles"][0]["items"][0].update(evidence=[]), "V4"),
    (lambda d: d["styles"][0]["items"][0].update(evidence=[{"block_idx": 1, "quote": "테스트코어"},
                                                           {"block_idx": 3, "quote": "버건디"},
                                                           {"block_idx": 4, "quote": "슬림"}]), "V4"),
    (lambda d: d["styles"][0]["items"][0].update(evidence=[{"block_idx": 4, "quote": "슬림"},
                                                           {"block_idx": 7, "quote": "미디 스커트"}]), "V4"),
])
def test_each_rule_violation(mutate, stage):
    doc = _llm_doc()
    mutate(doc)
    _ext, rep = _validated(doc)
    assert not rep.passed and rep.counts.get(stage, 0) >= 1, rep.errors


def test_adjacency_skips_image_blocks():
    # 블록 3(소제목)과 4(문단)는 인접, 4와 6은 이미지 5를 사이에 두지만 텍스트 순서로 인접
    doc = _llm_doc(items=[_item(evidence=[{"block_idx": 4, "quote": "버건디 셔츠는"}, {"block_idx": 6, "quote": "중간"}],
                                category_raw=None, fit_raw=None, fit=None)])
    _ext, rep = _validated(doc)
    assert rep.counts.get("V4", 0) == 0, rep.errors


def test_warnings_do_not_fail():
    doc = _llm_doc(seasons=[], items=[_item(raw_text="요약한 문장", patterns=[{"raw": "줄무늬", "value": "스트라이프"}],
                                             evidence=[{"block_idx": 4, "quote": "줄무늬"}], category_raw=None)])
    _ext, rep = _validated(doc)
    assert rep.passed, rep.errors
    assert rep.counts["W5"] >= 2  # seasons 없음 + raw_text 비부분문자열


def test_v2_v3_equivalent_to_validate_labels(tmp_path):
    """라벨 검증 스크립트(scripts/validate_labels.py)와 V2·V3 판정이 같다."""
    spec = importlib.util.spec_from_file_location("validate_labels", ROOT / "scripts" / "validate_labels.py")
    vl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vl)
    assert vl.squash(" 가 나\n다 ") == squash(" 가 나\n다 ")
    doc = _llm_doc(items=[_item(image_links=[], fit_raw="오버핏",
                                evidence=[{"block_idx": 3, "quote": "버건디 셔츠"}, {"block_idx": 4, "quote": "없는 인용"}])])
    pdoc, _ = postprocess(doc, ARTICLE)
    label = {"label_meta": {f: "x" for f in vl.META_FIELDS} | {"article_id": "t-001", "status": "silver"},
             "extraction": pdoc}
    path = tmp_path / "t-001.json"
    path.write_text(json.dumps(label, ensure_ascii=False), encoding="utf-8")
    l_err, l_warn = vl.validate(path, {"t-001": ARTICLE}, None)
    ext = ArticleExtraction.model_validate(pdoc)
    rep = validate_extraction(ext, ARTICLE)
    assert len([e for e in l_err if "quote" in e]) == rep.counts.get("V2", 0) == 1
    assert len([w for w in l_warn if "raw" in w]) == rep.counts.get("V3", 0) == 1


# ---------- 재시도·검토·출력 ----------
class _SeqBackend:
    def __init__(self, docs):
        self.docs = list(docs)
        self.prompts = []

    def run(self, prompt, schema, *, name, model, reasoning_effort, timeout_s):
        self.prompts.append(prompt)
        d = self.docs.pop(0) if len(self.docs) > 1 else self.docs[0]
        return BackendReply(data=copy.deepcopy(d), raw_text="{}", latency_s=1.25, thread_id=f"th-{len(self.prompts)}",
                            usage={"input_tokens": 100, "output_tokens": 10})


@pytest.fixture
def fake_llm(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "load_env", lambda: {})
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "calls.jsonl"))

    def install(docs):
        b = _SeqBackend(docs)
        client.register_backend("openai_api", b)
        return b

    yield install
    client.register_backend("openai_api", None)


def test_retry_once_then_pass(fake_llm):
    bad = _llm_doc()
    bad["styles"][0]["evidence"] = [{"block_idx": 1, "quote": "기사에 없는 문장"}]
    b = fake_llm([bad, _llm_doc()])
    out = extractor.extract_article_outcome(ARTICLE, backend="openai_api", model="m-1", prompt_version="extract-v1")
    assert out.passed and out.attempts == 2 and len(b.prompts) == 2
    assert "이전 출력의 검증 오류" in b.prompts[1] and "기사에 없는 문장" in b.prompts[1]
    doc = extractor.build_output_doc(out, ARTICLE)
    meta = doc["label_meta"]
    assert meta["status"] == "auto" and meta["labeler"] == "llm:openai_api/m-1" and meta["published"] == "2026-10-21"
    assert meta["prompt_version"] == "extract-v1" and meta["guideline_version"] == "0.3-draft"
    assert meta["postprocess"] == {"alias_map": True, "length_area": False}
    assert meta["validation"]["passed"] is True and meta["validation"]["attempts"] == 2
    assert meta["llm"]["thread_ids"] == ["th-1", "th-2"] and meta["llm"]["latency_s"] == 2.5
    ArticleExtraction.model_validate(doc["extraction"])
    assert doc["extraction"]["styles"][0]["items"][0]["image_links"] == [{"image_idx": 2, "basis": "adjacent"}]


def test_still_failing_goes_to_review(fake_llm):
    bad = _llm_doc(items=[_item(fit_raw="오버핏")])
    fake_llm([bad])
    out = extractor.extract_article_outcome(ARTICLE, backend="openai_api", model="m-1", prompt_version="extract-v1")
    assert not out.passed and out.attempts == 2 and out.counts["V3"] == 1
    with pytest.raises(extractor.ExtractionFailed):
        extractor.extract_article(ARTICLE, backend="openai_api", model="m-1", prompt_version="extract-v1")


def test_pydantic_failure_kept_as_raw(fake_llm):
    bad = _llm_doc()
    bad["styles"][0]["focus_attributes"] = [
        {"attribute": a, "level": "core", "evidence": [{"block_idx": 1, "quote": "화려한 컬러"}]}
        for a in ("color", "pattern", "detail", "fit")]  # core 4개 > 3
    fake_llm([bad])
    out = extractor.extract_article_outcome(ARTICLE, backend="openai_api", model="m-1", prompt_version="extract-v1")
    assert not out.passed and out.extraction is None and out.counts["V1"] >= 1
    doc = extractor.build_output_doc(out, ARTICLE)
    assert doc["extraction"]["article_id"] == "t-001" and doc["label_meta"]["validation"]["passed"] is False


def test_negative_article_empty_styles(fake_llm):
    fake_llm([{"styles": []}])
    ext = extractor.extract_article(ARTICLE, backend="openai_api", model="m-1", prompt_version="extract-v1")
    assert ext.styles == [] and ext.article_id == "t-001"


def test_length_area_rule_optional():
    skirt = _item(name="긴 스커트", raw_text="긴 스커트", category_raw="스커트", category_minor="스커트", colors=[],
                  fit_raw=None, fit=None, length_body_raw="긴", length_body="맥시", image_links=[],
                  evidence=[{"block_idx": 7, "quote": "미디 스커트"}])
    off, _ = postprocess(_llm_doc(items=[skirt]), ARTICLE)
    assert off["styles"][0]["items"][0]["length_body"] == "롱"  # 별칭 사전 '긴'→롱 (영역 무시)
    on, _ = postprocess(_llm_doc(items=[skirt]), ARTICLE, length_area=True)
    it = on["styles"][0]["items"][0]
    assert it["length_body"] == "맥시" and "length_area:length_body:롱->맥시" in it["review_flags"]
    coat = _item(category_minor="코트", length_body_raw=None, length_body="롱")
    on2, _ = postprocess(_llm_doc(items=[coat]), ARTICLE, length_area=True)
    assert on2["styles"][0]["items"][0]["length_body"] == "롱"  # 아우터는 롱 허용


def test_validator_uses_label_check_functions_and_fallback_agrees():
    from trend_agent.ingestion.extraction.validate import label_checks

    lib = label_checks()
    assert lib is not None and callable(lib.check_quotes) and callable(lib.check_raws)
    cases = [
        _llm_doc(),
        _llm_doc(items=[_item(fit_raw="오버핏", image_links=[])]),
        _llm_doc(items=[_item(image_links=[], evidence=[{"block_idx": 3, "quote": "버건디 셔츠"},
                                                       {"block_idx": 4, "quote": "없는 인용"}])]),
        _llm_doc(evidence=[{"block_idx": 0, "quote": "Brand A"}], items=[]),
    ]
    for doc in cases:
        pdoc, _ = postprocess(doc, ARTICLE)
        ext = ArticleExtraction.model_validate(pdoc)
        a = validate_extraction(ext, ARTICLE)
        b = validate_extraction(ext, ARTICLE, use_label_checks=False)
        for stage in ("V2", "V3"):
            assert a.counts.get(stage, 0) == b.counts.get(stage, 0), (stage, a.errors, b.errors)
