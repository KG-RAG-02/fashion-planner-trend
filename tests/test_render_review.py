"""render_review: 1차 검수 의견·음성 라벨 표시 (합성 입력, Vogue 원문 없음)"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("render_review", ROOT / "scripts" / "render_review.py")
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

ART = {"blocks": [{"idx": 0, "type": "paragraph", "text": "합성 문단 영."}, {"idx": 1, "type": "heading", "text": "합성 소제목"}]}


def meta(**kw):
    m = {"article_id": "000001", "url": "https://example.invalid/a", "title": "합성 기사", "published": "2026-01-01",
         "status": "silver", "labeler": "claude (T4)", "reviewer": None, "guideline_version": "0.3-draft",
         "labeled_at": "2026-11-08", "notes": ["메모 하나"]}
    m.update(kw)
    return m


def test_negative_label_shows_empty_styles_and_t4_review_with_approval_guide():
    doc = {"label_meta": meta(t4_review=[{"target": "기사", "opinion": "음성 확정 | 의견", "basis": "[0] 근거"}]),
           "extraction": {"article_id": "000001", "styles": []}}
    md = rr.render(doc, ART)
    assert md.index("## 1차 검수 의견") < md.index("## 사용자 승인 방법") < md.index("## 정답: Style 없음")
    assert "| 기사 | 음성 확정 \\| 의견 | [0] 근거 |" in md  # 표 안의 | 는 이스케이프
    assert "gold 전환은 **사용자만**" in md
    assert "[0] 합성 문단 영." in md and "**[1] 합성 소제목**" in md


def test_without_t4_review_output_has_no_review_section():
    doc = {"label_meta": meta(), "extraction": {"article_id": "000001", "styles": []}}
    md = rr.render(doc, ART)
    assert "1차 검수 의견" not in md and "사용자 승인 방법" not in md
    assert md.startswith("# 검수: 합성 기사")
