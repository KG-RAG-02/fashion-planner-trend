"""validate_labels: V2·V3 순수 함수(check_quotes·check_raws)와 validate() 동작 불변 (합성 입력, Vogue 원문 없음)"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from trend_agent.schemas.article_extraction import ArticleExtraction

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("validate_labels", ROOT / "scripts" / "validate_labels.py")
vl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vl)

ART = {"article_id": "000001", "blocks": [
    {"idx": 0, "type": "paragraph", "text": "올봄 합성 트렌드 가 떠올랐다. 빨간 니트 스웨터를 입는다."},
    {"idx": 1, "type": "image", "caption": "합성 캡션 문장"},
    {"idx": 2, "type": "heading", "text": "합성 소제목"},
    {"idx": 3, "type": "paragraph", "text": "넓은 바지에 체크 무늬를 더한다."},
]}


def item(name, raw_text, ev, **kw):
    d = {"name": name, "raw_text": raw_text, "evidence": [{"block_idx": b, "quote": q} for b, q in ev]}
    d.update(kw)
    return d


def ext(items, style_ev=((0, "합성 트렌드가 떠올랐다"),), focus_ev=None):
    s = {"name": "합성 트렌드", "name_origin": "article", "definition": "합성", "evidence": [{"block_idx": b, "quote": q} for b, q in style_ev], "items": items}
    if focus_ev:
        s["focus_attributes"] = [{"attribute": "color", "level": "core", "evidence": [{"block_idx": b, "quote": q} for b, q in focus_ev]}]
    return {"article_id": "000001", "styles": [s]}


GOOD = ext([item("빨간 니트", "빨간 니트 스웨터", [(0, "빨간 니트 스웨터를 입는다")], category_raw="니트 스웨터",
                 colors=[{"raw": "빨간", "value": "레드"}])])
BAD = ext([
    item("빨간 니트", "빨간 니트", [(0, "파란 니트 스웨터")], colors=[{"raw": "파란", "value": "블루"}]),   # V2 오류 + V3 경고
    item("근거 없음", "넓은 바지", []),                                                                    # evidence 없음
    item("이미지 근거", "체크", [(1, "합성 캡션 문장")], patterns=[{"raw": "체크", "value": "체크"}]),       # 이미지 블록 = 문단 없음
    item("넓은 바지", "넓은 바지", [(2, "합성 소제목"), (3, "넓은 바지에 체크 무늬를 더한다")], fit_raw="넓은", fit="와이드",
         styling_raw="허리끈"),                                                                             # 소제목 인용 OK, styling V3 경고
], style_ev=((0, "합성 트렌드가 떠올랐다"), (9, "없는 블록")), focus_ev=((3, "빨간"),))


def legacy_validate(doc, articles, version, stem):
    """리팩터링 전 validate()의 본문(커밋 3aab14f) 그대로 — 동작 불변 비교용."""
    squash = lambda s: re.sub(r"\s+", "", s)
    errors, warnings = [], []
    meta = doc.get("label_meta", {})
    for f in vl.META_FIELDS:
        if f not in meta:
            errors.append(f"label_meta.{f} 없음")
    if meta.get("status") not in ("silver", "gold"):
        errors.append(f"status가 silver|gold가 아님: {meta.get('status')}")
    if meta.get("article_id") != stem:
        errors.append("파일 이름과 label_meta.article_id가 다름")
    if version and meta.get("guideline_version") != version:
        warnings.append(f"guideline_version {meta.get('guideline_version')} != manifest {version}")
    try:
        ext_ = ArticleExtraction.model_validate(doc["extraction"])
    except Exception as e:
        return [f"스키마 오류: {e}"] + errors, warnings
    art = articles.get(stem)
    if art is None:
        return errors + ["원본 기사를 찾을 수 없음 (data/raw/vogue_kr)"], warnings
    text_by_idx = {b["idx"]: b.get("text", "") for b in art["blocks"] if b["type"] != "image"}

    def check_quote(where, block_idx, quote):
        text = text_by_idx.get(block_idx)
        if text is None:
            errors.append(f"{where}: block_idx {block_idx}에 문단이 없음")
        elif squash(quote) not in squash(text):
            errors.append(f"{where}: quote가 block {block_idx}에 없음: {quote[:40]}")

    for s in ext_.styles:
        for e in s.evidence:
            check_quote(f"Style[{s.name}].evidence", e.block_idx, e.quote)
        for d in s.description:
            for e in d.evidence:
                check_quote(f"Style[{s.name}].description", e.block_idx, e.quote)
        for f in s.focus_attributes:
            for e in f.evidence:
                check_quote(f"Style[{s.name}].focus[{f.attribute.value}]", e.block_idx, e.quote)
        for it in s.items:
            if not it.evidence:
                errors.append(f"Item[{it.name}]: evidence 없음")
            for e in it.evidence:
                check_quote(f"Item[{it.name}].evidence", e.block_idx, e.quote)
            ev_text = squash("".join(text_by_idx.get(e.block_idx, "") for e in it.evidence))
            raws = [it.category_raw, it.fit_raw, it.shape_raw, it.length_body_raw, it.length_sleeve_raw, it.styling_raw, it.styling_accessory]
            raws += [c.raw for c in it.colors] + [m.raw for m in it.materials] + [p.raw for p in it.patterns] + [d.raw for d in it.details]
            for r in raws:
                if r and squash(r) not in ev_text:
                    warnings.append(f"Item[{it.name}]: raw '{r}'가 근거 문단에 없음 (지시 표현이면 notes에 적을 것)")
    return errors, warnings


def meta(**kw):
    m = {"article_id": "000001", "url": "u", "title": "t", "status": "silver", "labeler": "x", "reviewer": None,
         "guideline_version": "0.3-draft", "labeled_at": "2026-11-08", "notes": []}
    m.update(kw)
    return m


def test_check_quotes_and_raws_on_clean_extraction_are_empty():
    e = ArticleExtraction.model_validate(GOOD)
    tb = vl.text_blocks(ART)
    assert 1 not in tb and tb[2] == "합성 소제목"
    assert vl.check_quotes(e, tb) == [] and vl.check_raws(e, tb) == []


def test_check_quotes_reports_v2_without_missing_evidence_rule():
    e = ArticleExtraction.model_validate(BAD)
    errs = vl.check_quotes(e, vl.text_blocks(ART))
    assert errs == [
        "Style[합성 트렌드].evidence: block_idx 9에 문단이 없음",
        "Style[합성 트렌드].focus[color]: quote가 block 3에 없음: 빨간",
        "Item[빨간 니트].evidence: quote가 block 0에 없음: 파란 니트 스웨터",
        "Item[이미지 근거].evidence: block_idx 1에 문단이 없음",
    ]  # 'evidence 없음'은 V2가 아니라 라벨 검증(validate)의 규칙이라 넣지 않는다


def test_check_raws_reports_v3_warnings():
    w = vl.check_raws(ArticleExtraction.model_validate(BAD), vl.text_blocks(ART))
    assert w == [
        "Item[빨간 니트]: raw '파란'가 근거 문단에 없음 (지시 표현이면 notes에 적을 것)",
        "Item[이미지 근거]: raw '체크'가 근거 문단에 없음 (지시 표현이면 notes에 적을 것)",
        "Item[넓은 바지]: raw '허리끈'가 근거 문단에 없음 (지시 표현이면 notes에 적을 것)",
    ]


@pytest.mark.parametrize("doc", [
    {"label_meta": meta(), "extraction": GOOD},
    {"label_meta": meta(), "extraction": BAD},
    {"label_meta": meta(status="draft", guideline_version="0.2-draft"), "extraction": BAD},
    {"label_meta": {"article_id": "000001"}, "extraction": GOOD},
    {"label_meta": meta(), "extraction": {"article_id": "000001", "styles": [{"name": "x"}]}},  # 스키마 오류
])
def test_validate_output_unchanged_after_refactor(tmp_path, doc):
    p = tmp_path / "000001.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    arts = {"000001": ART}
    assert vl.validate(p, arts, "0.3-draft") == legacy_validate(doc, arts, "0.3-draft", "000001")
    assert vl.validate(p, {}, None) == legacy_validate(doc, {}, None, "000001")  # 원본 기사 없음 경로
