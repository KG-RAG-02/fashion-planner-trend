"""적재 v1.1 (Neo4j 없이): 기사 출처 선택·이미지 맵·메타 어댑터·auto 근사 재판정."""
from conftest import synthetic_crawl, synthetic_label
from fashion_planner_trend.graph import loader
from fashion_planner_trend.schemas.article_extraction import ArticleExtraction
from fashion_planner_trend.schemas.graph_schema import item_record_from_extraction


def test_article_sources_default_prefers_extractions_and_skips_review(tmp_path):
    lab, ext = tmp_path / "labels", tmp_path / "ext"
    (ext / "_review").mkdir(parents=True)
    lab.mkdir()
    for d, aid in ((lab, "1"), (lab, "2"), (ext, "2"), (ext, "3"), (ext / "_review", "4")):
        (d / f"{aid}.json").write_text("{}")
    got = loader.article_sources(lab, ext)  # 기본: 같은 기사는 추출 결과가 이긴다 (라벨은 평가 전용)
    assert {k: v.parent.name for k, v in got.items()} == {"1": "labels", "2": "ext", "3": "ext"}
    assert loader.article_sources(None, ext).keys() == {"3", "2"}  # 라벨 폴더를 주지 않으면 라벨은 적재되지 않는다
    assert loader.article_sources(lab, ext, prefer="labels")["2"].parent.name == "labels"
    assert list(loader.article_sources(lab, ext, article_ids=["3"])) == ["3"]


def test_image_map_and_item_image_urls():
    m = loader.image_map_of(synthetic_crawl())
    assert m == {0: "https://img.example.invalid/0.jpg", 1: "https://img.example.invalid/1.jpg"}
    ext = ArticleExtraction.model_validate(synthetic_label()["extraction"])
    rec = item_record_from_extraction(ext.styles[0].items[0], "900001-s0-i0", "900001", image_urls=m)
    assert rec.node.image_urls == ["https://img.example.invalid/0.jpg"] and rec.node.image_basis == ["adjacent"]


def test_article_props_meta_adapter():
    auto = loader.article_props(synthetic_label(status="auto")["label_meta"], "900001")
    assert auto["source"] == "extraction" and auto["labeler"] == "llm:openai_api/test-model"
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
