"""트렌드 언급 집계(term_mentions) — 합성 기사·추출만."""
from __future__ import annotations

import csv

from trend_agent.ingestion.extraction.terms import (
    build_term_mentions, core_words, parse_kfashion_substyles, title_quotes,
)
from trend_agent.schemas.graph_schema import normalize_term


def _art(aid, title, texts, published="2026-10-21T00:00:00+00:00", season=None, caption=None):
    blocks = [{"type": "paragraph", "idx": i + 1, "text": t} for i, t in enumerate(texts)]
    if caption:
        blocks.insert(0, {"type": "image", "idx": 0, "caption": caption})
    return {"article_id": aid, "title": title, "published_at": published, "season": season, "blocks": blocks}


ARTS = [
    _art("a1", "올해는 ‘테스트코어’", ["테스트 코어가 유행입니다.", "포스트모던 감성과 모던한 재킷.", "스코어가 높다."],
         season={"value": "2027SS", "source": "tag"}, caption="테스트코어 캡션은 세지 않는다"),
    _art("a2", "다른 기사", ["테스트코어의 시대는 끝났다. 마이클 코어스 쇼."], published="2026-09-02T00:00:00+00:00"),
    _art("a3", "‘이것’만 있으면 된다", ["관계없는 문단."], published="2026-08-01T00:00:00+00:00"),
]
EXTS = {"a1": {"styles": [{"name": "테스트코어", "aliases": ["Test Core"], "seasons": [{"year": 2027, "season": "SS"}],
                           "evidence": [{"block_idx": 3, "quote": "x"}]}]}}


def _rows(**kw):
    return {r["norm"]: r for r in build_term_mentions(ARTS, EXTS, seeds=("드뮤어",), **kw)}


def test_helpers():
    assert title_quotes("A ‘가’ 'B C' \"D\" “E”") == ["가", "B C", "D", "E"]
    assert core_words("놈코어 와 해적 코어, 마이클 코어스, 스코어") == ["놈코어", "해적코어"]
    subs = parse_kfashion_substyles()
    assert len(subs) == 23 and subs[0] == "클래식" and "키치/키덜트" in subs and subs[-1] == "스트리트"


def test_style_mention_title_roles_and_counts():
    rows = _rows()
    r = rows[normalize_term("테스트코어")]
    assert r["term_id"] == "term:테스트코어" and r["sources"][:3] == ["extracted_style", "title_quote", "core_suffix"]
    assert r["article_count"] == 2 and r["style_article_count"] == 1 and r["title_article_count"] == 1
    assert r["mention_count"] == 3  # 제목 1 + a1 문단 1 (공백 유연) + a2 문단 1 — 캡션 제외
    ev = {(e["article_id"], e["block_idx"]): e for e in r["evidence"]}
    assert ev[("a1", None)]["role"] == "title"
    assert ev[("a1", 1)]["role"] == "style" and ev[("a1", 1)]["style_id"] == "a1-s0"
    assert ev[("a1", 3)]["role"] == "style"  # 용어가 글자로 없어도 Style 근거 블록은 넣는다
    assert ev[("a1", 3)]["literal"] is False and ev[("a1", 1)]["literal"] is True and ev[("a1", None)]["literal"] is True
    assert ev[("a1", 1)]["seasons"] == ["2027SS"] and ev[("a1", 1)]["season_source"] == "style"
    assert ev[("a2", 1)]["role"] == "mention" and ev[("a2", 1)]["style_id"] is None  # "끝났다" 언급도 mention
    assert r["months"] == {"2026-09": 1, "2026-10": 1} and r["first_seen"] == "2026-09-02"
    assert "evidence" in r and all("quote" not in e for e in r["evidence"])  # 인용 문장은 넣지 않는다


def test_word_boundary_alias_and_stopwords():
    rows = _rows()
    modern = rows["모던"]
    assert modern["article_count"] == 1 and modern["mention_count"] == 1  # "포스트모던" 제외, "모던한" 포함
    assert "스코어" not in rows and "마이클코어" not in rows
    assert rows["testcore"]["surfaces"] == ["Test Core"]  # alias는 별도 norm 행


def test_seed_zero_row_and_generic_title_quote():
    rows = _rows()
    assert rows["드뮤어"]["article_count"] == 0 and rows["드뮤어"]["evidence"] == []
    assert rows["이것"]["generic"] is True


def test_inventory_merge(tmp_path):
    inv = tmp_path / "inventory.csv"
    with open(inv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["keyword", "norm", "generic"])
        w.writeheader()
        w.writerow({"keyword": "관계없는", "norm": "x", "generic": "False"})
        w.writerow({"keyword": "문단", "norm": "문단", "generic": "True"})
    rows = _rows(inventory=inv)
    assert rows["관계없는"]["sources"] == ["t2_keyword"] and rows["관계없는"]["article_count"] == 1
    assert "문단" not in rows  # generic=true 행은 쓰지 않는다
