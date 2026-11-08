"""적재 v1.1 (Neo4j 없이): 기사 출처 선택·이미지 맵·메타 어댑터·auto 근사 재판정·Term 재구성 행·term_map 형식."""
import json

import pytest

from conftest import synthetic_crawl, synthetic_label
from trend_agent.graph import loader
from trend_agent.graph.terms import build_term_rows, maps_to_edges
from trend_agent.resolver.term_map import TermMapRow, append_rows, find_profile, read_term_map
from trend_agent.schemas.article_extraction import ArticleExtraction
from trend_agent.schemas.graph_schema import item_record_from_extraction


def test_article_sources_prefer_labels_and_skip_review(tmp_path):
    lab, ext = tmp_path / "labels", tmp_path / "ext"
    (ext / "_review").mkdir(parents=True)
    lab.mkdir()
    for d, aid in ((lab, "1"), (lab, "2"), (ext, "2"), (ext, "3"), (ext / "_review", "4")):
        (d / f"{aid}.json").write_text("{}")
    got = loader.article_sources(lab, ext)
    assert {k: v.parent.name for k, v in got.items()} == {"1": "labels", "2": "labels", "3": "ext"}
    assert loader.article_sources(lab, ext, prefer="extractions")["2"].parent.name == "ext"
    assert list(loader.article_sources(lab, ext, article_ids=["3"])) == ["3"]


def test_image_map_and_item_image_urls():
    m = loader.image_map_of(synthetic_crawl())
    assert m == {0: "https://img.example.invalid/0.jpg", 1: "https://img.example.invalid/1.jpg"}
    ext = ArticleExtraction.model_validate(synthetic_label()["extraction"])
    rec = item_record_from_extraction(ext.styles[0].items[0], "900001-s0-i0", "900001", image_urls=m)
    assert rec.node.image_urls == ["https://img.example.invalid/0.jpg"] and rec.node.image_basis == ["adjacent"]


def test_article_props_meta_adapter():
    auto = loader.article_props(synthetic_label(status="auto")["label_meta"], "900001")
    assert auto["source"] == "extraction" and auto["labeler"] == "llm:codex_cli/test-model"
    assert auto["prompt_version"] == "extract-test" and auto["label_status"] == "auto"
    lab = loader.article_props({"status": "silver"}, "900001", synthetic_crawl())
    assert lab["source"] == "label" and lab["title"] == "합성 기사" and lab["published_at"] == "2026-10-01"


def test_mark_auto_approx_rejudges_raw():
    ext = ArticleExtraction.model_validate(synthetic_label(status="auto", color_raw="차콜 그레이")["extraction"])
    it = item_record_from_extraction(ext.styles[0].items[0], "x-s0-i0", "x")
    assert not it.colors[0].approx
    assert loader.mark_auto_approx(it) == 1 and it.colors[0].approx  # 차콜 그레이 → 그레이(근사)
    assert not it.materials[0].approx                                # 면 = 표준
    skirt = item_record_from_extraction(ext.styles[0].items[1], "x-s0-i1", "x")
    assert loader.mark_auto_approx(skirt) == 0                       # 플리츠 = 표준 (주름이었다면 근사)


def _rows():
    return [
        TermMapRow(row="alias", term="Quiet Luxury", kind="translation", origin="manual", article_id="900001", style_norm="테스트럭셔리"),
        TermMapRow(row="alias", term="없는 별칭", kind="alias", origin="manual", article_id="900001", style_norm="없는트렌드명"),
        TermMapRow(row="mention", term="고프코어", kind="trend_word", origin="term_mentions", article_count=3),
        TermMapRow(row="profile", term="드뮤어", kind="trend_word", origin="llm", backend="codex_cli", model="m", prompt_version="tier4.v1",
                   context_sha="abc", created_at="2026-11-08T21:00:00+09:00",
                   profile={"recognized": True, "focus": {"color": "core", "material": "supporting"},
                            "items": [{"category_major": "상의", "level": "core", "colors": [{"value": "베이지", "value_level": "primary"}],
                                       "materials": [{"value": "면", "value_level": "secondary"}], "details": [], "patterns": [], "fit": "레귤러"}]}),
    ]


def test_build_term_rows():
    style_aliases = [{"sid": "900001-s0", "aid": "900001", "name_norm": "테스트럭셔리", "aliases": ["콰이어트 럭셔리", "테스트 럭셔리"],
                      "source": "label"}]
    b = build_term_rows(_rows(), style_aliases)
    ids = [t["id"] for t in b["terms"]]
    assert ids == sorted(ids) and "term:콰이어트럭셔리" in ids and "term:테스트럭셔리" not in ids  # 이름 자신은 Term이 아니다
    by = {t["id"]: t for t in b["terms"]}
    assert by["term:고프코어"]["article_count"] == 3 and by["term:quietluxury"]["kind"] == "translation"
    assert json.loads(by["term:드뮤어"]["profile_json"])["items"][0]["fit"] == "레귤러"
    assert {(a["term_id"], a["style_norm"]) for a in b["aliases"]} == {
        ("term:콰이어트럭셔리", "테스트럭셔리"), ("term:quietluxury", "테스트럭셔리"), ("term:없는별칭", "없는트렌드명")}
    maps = {(lab, m["value"], m["props"].get("level"), m["props"].get("value_level")) for lab, ms in b["maps"].items() for m in ms}
    assert maps == {("Category", "상의", "core", None), ("Color", "베이지", "core", "primary"), ("Material", "면", "supporting", "secondary")}
    assert all("weight" not in m["props"] for ms in b["maps"].values() for m in ms)  # 수치 없음


def test_maps_to_skips_unrecognized():
    b = build_term_rows([TermMapRow(row="profile", term="모름", kind="trend_word", origin="llm", backend="b", prompt_version="v",
                                    context_sha="s", profile={"recognized": False, "items": []})])
    assert b["maps"] == {} and b["terms"][0]["id"] == "term:모름"
    assert maps_to_edges({"items": [{"category_major": "하의", "level": "supporting"}]}, None, None)[0].attribute == "category"


def test_term_map_rows_validate_and_append_idempotent(tmp_path):
    p = tmp_path / "terms" / "term_map.jsonl"
    with pytest.raises(ValueError):
        TermMapRow(row="alias", term="x y", norm="x", kind="alias", origin="manual", style_norm="s")  # norm 불일치
    with pytest.raises(ValueError):
        TermMapRow(row="alias", term="x", kind="alias", origin="manual")  # style_norm 없음
    with pytest.raises(ValueError):
        TermMapRow(row="profile", term="x", kind="trend_word", origin="llm", backend="b")  # 캐시 키 부족
    assert append_rows(_rows(), p) == 4 and append_rows(_rows(), p) == 0
    rows = read_term_map(p)
    assert len(rows) == 4 and rows[0].norm == "quietluxury"
    assert find_profile(rows, "드뮤어", "codex_cli", "m", "tier4.v1", "abc").profile["recognized"]
    assert find_profile(rows, "드뮤어", "codex_cli", "m", "tier4.v1", "other") is None
    p.write_text(p.read_text() + "{broken\n")
    with pytest.raises(ValueError, match=":5:"):
        read_term_map(p)
