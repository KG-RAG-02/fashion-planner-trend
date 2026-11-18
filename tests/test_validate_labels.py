"""validate_labels: V2·V3 순수 함수(check_quotes·check_raws)와 validate() (합성 입력, 기사 원문 없음)"""
import importlib.util
import json
from pathlib import Path

from fashion_planner_trend.schemas.article_extraction import ArticleExtraction

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


def meta(**kw):
    m = {"article_id": "000001", "url": "u", "title": "t", "status": "silver", "labeler": "x", "reviewer": None,
         "labeled_at": "2026-11-08", "notes": []}
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


def _validate(tmp_path, doc, arts):
    p = tmp_path / "000001.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return vl.validate(p, arts)


def test_validate_clean_label(tmp_path):
    assert _validate(tmp_path, {"label_meta": meta(), "extraction": GOOD}, {"000001": ART}) == ([], [])


def test_validate_reports_quotes_missing_evidence_and_raws(tmp_path):
    e = ArticleExtraction.model_validate(BAD)
    errors, warnings = _validate(tmp_path, {"label_meta": meta(), "extraction": BAD}, {"000001": ART})
    assert set(vl.check_quotes(e, vl.text_blocks(ART))) <= set(errors)
    assert "Item[근거 없음]: evidence 없음" in errors       # 라벨 검증에만 있는 규칙
    assert warnings == vl.check_raws(e, vl.text_blocks(ART))


def test_validate_meta_status_schema_and_missing_article(tmp_path):
    errors, _ = _validate(tmp_path, {"label_meta": meta(status="draft"), "extraction": GOOD}, {"000001": ART})
    assert errors == ["status가 silver|gold가 아님: draft"]
    errors, _ = _validate(tmp_path, {"label_meta": {"article_id": "000001"}, "extraction": GOOD}, {"000001": ART})
    assert [e for e in errors if e.startswith("label_meta.")] == [f"label_meta.{f} 없음" for f in vl.META_FIELDS if f != "article_id"]
    errors, _ = _validate(tmp_path, {"label_meta": meta(), "extraction": {"article_id": "000001", "styles": [{"name": "x"}]}}, {})
    assert errors[0].startswith("스키마 오류")
    errors, _ = _validate(tmp_path, {"label_meta": meta(), "extraction": GOOD}, {})
    assert errors == ["원본 기사를 찾을 수 없음 (data/raw)"]
