"""Resolver 순수 함수 (Neo4j·LLM 없이): Tier2 분절 · Tier4 스키마·검증·수치화 · 트렌드 Top-K."""
import json

import pytest

from conftest import product
from trend_agent.resolver import tier4 as t4
from trend_agent.resolver.normalize import Segmenter
from trend_agent.resolver.profile import VirtualItemSpec
from trend_agent.retrieval.scoring import rank_products
from trend_agent.retrieval.trend_topk import ItemRanking, dedup_key, trend_topk
from trend_agent.retrieval.weights import load_weights
from trend_agent.schemas.graph_schema import AttrEdge, ItemNode, ItemRecord, normalize_term

W = load_weights()
SEG = Segmenter()


@pytest.mark.parametrize("term,code", [
    ("퍼플", "vocab_full"), ("플리츠 스커트", "vocab_full"), ("플리츠스커트", "vocab_full"), ("레더 재킷", "vocab_full"),
    ("후드 집업", "vocab_full"), ("카키 야상", "vocab_full"), ("미니 스커트", "vocab_full"),
    ("드뮤어", "vocab_none"), ("조용한 럭셔리", "vocab_none"), ("고프코어", "vocab_none"),
    ("주름 스커트", "vocab_partial"), ("슬림 팬츠", "vocab_partial"), ("주름", "vocab_approx_only"),
])
def test_tier2_segmentation_codes(term, code):
    assert SEG.segment(normalize_term(term)).code == code


def test_tier2_tokens_and_priority():
    seg = SEG.segment(normalize_term("레더 재킷"))
    assert [(t.attr, t.value) for t in seg.usable] == [("material", "가죽"), ("category", "재킷")]
    mini = SEG.segment(normalize_term("미니 스커트"))
    assert [t.attr for t in mini.unscored] == ["body_length"]  # 아는 단어지만 점수에 쓰지 않는다


# ---------- Tier4 ----------
def blocks():
    return [t4.ContextBlock(ref="900001:3", article_id="900001", block_idx=3, text="드뮤어한 베이지 셔츠가 돌아왔다.", role="mention")]


def good(**over):
    d = {"term": "드뮤어", "recognized": True, "summary": "단정하고 절제된 차림",
         "focus": {"color": "core", "material": "supporting", "pattern": None, "detail": None, "fit": "core", "category": None},
         "items": [{"category_major": "상의", "category_minor": "셔츠", "level": "core",
                    "colors": [{"value": "베이지", "value_level": "primary"}, {"value": "화이트", "value_level": "secondary"}],
                    "materials": [{"value": "면", "value_level": "primary"}], "patterns": [],
                    "details": [{"value": "collar:셔츠칼라", "value_level": "secondary"}] if "collar:셔츠칼라" in t4.DETAIL_IDS else [],
                    "fit": "노멀"},
                   {"category_major": "하의", "category_minor": "스커트", "level": "supporting", "colors": [], "materials": [],
                    "patterns": [], "details": [], "fit": None}],
         "evidence": [], "no_evidence": True}
    d.update(over)
    return d


def test_schema_is_closed_vocabulary_without_numbers():
    s = t4.tier4_schema()
    text = json.dumps(s, ensure_ascii=False)
    assert '"number"' not in text and '"integer"' not in text  # LLM은 숫자를 내지 않는다
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    item = s["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"]) and len(item["properties"]["colors"]["items"]["properties"]["value"]["enum"]) == 21
    assert None in item["properties"]["category_minor"]["enum"]


def test_check_profile_ok_and_errors():
    p, errs = t4.check_profile(good(), [])
    assert p is not None and errs == []
    _, errs = t4.check_profile(good(focus={a: "core" for a in t4.FOCUS_ATTRS}), [])
    assert any("core가 6개" in e for e in errs)
    bad_minor = good()
    bad_minor["items"][0]["category_major"] = "하의"
    assert any("상분류는 상의" in e for e in t4.check_profile(bad_minor, [])[1])
    _, errs = t4.check_profile(good(evidence=[{"ref": "900001:3", "quote": "베이지 셔츠가"}], no_evidence=False), blocks())
    assert errs == []
    _, errs = t4.check_profile(good(evidence=[{"ref": "900001:3", "quote": "지어낸 문장"}], no_evidence=False), blocks())
    assert any("원문에 없다" in e for e in errs)
    _, errs = t4.check_profile(good(evidence=[{"ref": "999:1", "quote": "x"}], no_evidence=False), blocks())
    assert any("제공한 블록이 아니다" in e for e in errs)
    assert t4.check_profile({"term": "x", "recognized": True, "items": [{"category_major": "신발"}]}, [])[0] is None
    unk, errs = t4.check_profile({"term": "x", "recognized": False, "items": [], "evidence": [], "no_evidence": True}, [])
    assert unk is not None and not unk.recognized and errs == []


def test_numericization_by_code():
    p, _ = t4.check_profile(good(), [])
    vp = t4.to_virtual(p, "드뮤어", "드뮤어", [])
    assert vp.focus_core == ["color", "fit"] and vp.focus_supporting == ["material"] and vp.value_weighted
    vw = vp.items[0].value_weights(W)
    assert vw["color"] == {"베이지": W.tier4.value_weights["primary"], "화이트": W.tier4.value_weights["secondary"]}
    assert vp.item_focus(vp.items[0], W) == W.tier4.item_focus["core"]
    assert vp.item_focus(vp.items[1], W) == W.tier4.item_focus["supporting"]
    assert vp.style_id == "virtual:4:드뮤어" and vp.item_id(1) == "virtual:4:드뮤어-i1"


def test_context_blocks_and_sha():
    mentions = t4.read_mentions(None)
    assert mentions == {}
    crawl = {"900001": {"title": "T", "blocks": [{"idx": 3, "text": "드뮤어한 베이지 셔츠"}]}}
    rows = {"드뮤어": {"term": "드뮤어", "evidence": [{"article_id": "900001", "block_idx": 3, "role": "mention"},
                                                    {"article_id": "900001", "block_idx": None, "role": "title"},
                                                    {"article_id": "900001", "block_idx": 99}]}}
    bs = t4.context_blocks("드뮤어", rows, crawl)
    assert [b.ref for b in bs] == ["900001:3"] and bs[0].title == "T"
    assert t4.context_sha(bs) != t4.context_sha([]) and t4.context_sha(bs) == t4.context_sha(list(bs))
    assert "없음" in t4.render_prompt("드뮤어", []) and "900001:3" in t4.render_prompt("드뮤어", bs)


# ---------- 트렌드 Top-K ----------
def _item(iid, color):
    return ItemRecord(node=ItemNode(id=iid, name=iid, raw_text="", article_id="a"), category_major="상의",
                      colors=[AttrEdge(value=color)])


def test_trend_topk_dedup_and_item_focus():
    ps = [product("p1", "상의", colors=["블랙"]), product("p2", "상의", colors=["블랙"], image_status="ok", image_url="u"),
          product("s1", "상의", colors=["블랙"], source="musinsa_snap", snap_id=7),
          product("s2", "상의", colors=["블랙"], source="musinsa_snap", snap_id=7),
          product("c1", "상의", colors=["화이트"], parent_id="p9"), product("c2", "상의", colors=["화이트"], parent_id="p9"),
          product("d1", "상의", colors=["화이트"], dup_of="p1")]
    a = ItemRanking("i-a", "A", 1.0, rank_products(_item("i-a", "블랙"), ps, W))
    b = ItemRanking("i-b", "B", 0.5, rank_products(_item("i-b", "화이트"), ps, W))
    picks = trend_topk([a, b], k=10)
    ids = [p.product_id for p in picks]
    assert ids == ["p1", "p2", "s1", "c1"]  # s2(같은 snap)·c2(같은 parent)·d1(dup_of p1) 합쳐짐
    p1 = picks[0]
    assert p1.trend_score == 1.0 and p1.item_id == "i-a" and p1.also_from_items == ["i-b"]  # d1이 i-b에서 와서 합쳐짐
    assert picks[3].trend_score == 0.5 and picks[3].item_focus == 0.5 and [p.rank for p in picks] == [1, 2, 3, 4]
    assert [p.product_id for p in trend_topk([a, b], k=10, image_first=True)][:2] == ["p2", "p1"]
    assert dedup_key(rank_products(_item("x", "블랙"), [ps[2]], W)[0]) == ("snap", 7)


def test_virtual_spec_record():
    spec = VirtualItemSpec(category_major="상의", category_minor="셔츠", colors=[("베이지", "primary")],
                           details=[("detail:플리츠", "secondary")], fit="루즈")
    rec = spec.record("virtual:4:x-i0", "x · 셔츠")
    assert rec.category_major.value == "상의" and rec.details[0].value == "플리츠" and rec.node.fit.value == "루즈"
    assert spec.value_weights(W) == {"color": {"베이지": 1.0}, "detail": {"detail:플리츠": 0.5}, "fit": {"루즈": 1.0}}
