"""표현층(HTML 갤러리·Obsidian 내보내기·트렌드 요약표) — 합성 결과 JSON, Vogue 원문 없음."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rtr = _load("render_trend_result")
exo = _load("export_obsidian")
rts = _load("render_trend_summary")

from trend_agent.schemas.trend_analysis_result import TrendAnalysisResult  # noqa: E402

VOGUE_IMG = "https://img.vogue.co.kr/vogue/2026/11/x.jpg"


def result_dict():
    prod = lambda **kw: {"source": "otpensource", "kind": "catalog_item", "score": 0.5, **kw}  # noqa: E731
    return {
        "request_id": "req-test", "status": "ok",
        "trends": [{
            "style": {"id": "100-s0", "name": "테스트: 코어 #1", "name_origin": "article", "definition": "정의",
                      "description": [{"text": "설명", "evidence_ids": ["trend:100:3:abcd1234"]}], "seasons": ["2027SS"]},
            "match_mode": "graph", "qualification": "article_evidence", "term": "테스트 코어",
            "items": [{"id": "100-s0-i0", "name": "블랙 스커트", "category_major": "하의", "category_minor": "스커트",
                       "colors": ["블랙"], "image_urls": [VOGUE_IMG], "evidence_ids": ["trend:100:3:abcd1234"],
                       "similar_products": [
                           prod(product_id="otpensource:f.json:1", name="A/B [스커트]", image_url="http://cdn/a.jpg",
                                image_status="ok", image_scope="catalog", has_image=True, colors=["블랙"], score=0.9),
                           prod(product_id="otpensource:f.json:2#1", name="세트 스커트", image_url="http://cdn/b.jpg",
                                image_status="broken", image_scope="full_set", parent_id="otpensource:f.json:2"),
                           {"product_id": "musinsa_snap:7:하의", "source": "musinsa_snap", "kind": "snap_garment",
                            "snap_id": 7, "image_scope": "full_snap", "image_status": "ok", "multi_wearer_suspected": True,
                            "has_image": True, "score": 0.4, "category_major": "하의", "category_minor": "스커트"},
                       ]}]}],
        "evidence": [{"evidence_id": "trend:100:3:abcd1234", "article_id": "100", "title": "제목", "url": "https://www.vogue.co.kr/a",
                      "block_idx": 3, "quote": "인용"}],
        "resolutions": [{"term": "테스트 코어", "norm": "테스트코어", "status": "resolved", "tier": 1, "match_mode": "graph",
                         "style_ids": ["100-s0"], "top_products": [
                             {**prod(product_id="otpensource:f.json:9", name="이미지없음", image_status="broken", has_image=False),
                              "rank": 1, "trend_score": 0.8, "item_id": "100-s0-i0", "item_name": "블랙 스커트", "item_focus": 1.0},
                             {"product_id": "musinsa_snap:7:하의", "source": "musinsa_snap", "kind": "snap_garment", "snap_id": 7,
                              "image_status": "ok", "has_image": True, "rank": 2, "trend_score": 0.4, "item_id": "100-s0-i0",
                              "item_name": "블랙 스커트", "item_focus": 1.0}]}],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / "images" / "snap").mkdir(parents=True)
    (shared / "images" / "snap" / "7.jpg").write_bytes(b"\xff\xd8fakejpeg")
    monkeypatch.setenv("SHARED", str(shared))
    res = TrendAnalysisResult.model_validate(result_dict())
    return tmp_path, shared, res


def test_html_links_vogue_images_only_and_points_snaps_to_original(env):
    tmp, shared, res = env
    out = tmp / "results"
    out.mkdir()
    page = rtr.render(res, "0" * 64, out, note_paths={"musinsa_snap:7:하의": "40_Products/snap-000007-하의.md"})
    assert f'href="{VOGUE_IMG}"' in page and f'src="{VOGUE_IMG}"' not in page  # G0-⑤ 링크만
    assert 'src="../shared/images/snap/7.jpg"' in page  # 원본 사진 1장, 상대 경로
    assert "착장 원본 사진 — 이 의류: 하의" in page and "여러 명이 찍힌 사진일 수 있음" in page
    assert "이미지 깨짐" in page and "세트 이미지" not in page.split("이미지 깨짐")[0][-300:]
    assert "obsidian://open?vault=_vault&amp;file=40_Products%2Fsnap-000007-" in page
    assert page == rtr.render(res, "0" * 64, out, note_paths={"musinsa_snap:7:하의": "40_Products/snap-000007-하의.md"})
    topk = page.split("트렌드 Top-K")[1]
    assert topk.index("#2 ·") < topk.index("#1 ·")  # 표시만 이미지 있는 것 먼저, 순위 번호는 그대로


def test_obsidian_export_rules(env):
    tmp, shared, res = env
    vault = tmp / "vault"
    rep = exo.export([(res, "a" * 64)], vault)["report"]
    assert rep["notes"] == {"trends": 1, "items": 1, "products": 4, "attributes": 2, "snaps": 1}
    snap_note = (vault / "40_Products" / "snap-000007-하의.md").read_text(encoding="utf-8")
    assert 'image: "[[assets/snap/7.jpg]]"' in snap_note and "snap_row: 7" in snap_note and "multi_wearer_suspected: true" in snap_note
    assert (vault / "assets/snap/7.jpg").read_bytes() == b"\xff\xd8fakejpeg" and not (vault / "assets/snap/7.jpg").is_symlink()
    prods = {p.name: p.read_text(encoding="utf-8") for p in (vault / "40_Products").glob("*.md")}
    cat = next(t for n, t in prods.items() if n.startswith("A B"))
    assert 'image: "http://cdn/a.jpg"' in cat and 'product_id: "otpensource:f.json:1"' in cat and "score: 0.9" in cat
    broken = next(t for n, t in prods.items() if n.startswith("세트 스커트"))
    assert "\nimage:" not in broken and 'parent_id: "otpensource:f.json:2"' in broken  # 깨진 이미지는 카드에 안 건다
    item = next((vault / "20_Items").glob("*.md")).read_text(encoding="utf-8")
    assert "[[30_Attributes/color/블랙|블랙]]" in item and VOGUE_IMG in item and "![" not in item.split("Vogue 이미지")[1]
    trend_files = list((vault / "10_Trends").glob("*.md"))
    assert [p.name for p in trend_files] == ["테스트 코어 1.md"]  # 금지 문자 제거
    canvas = json.loads(next((vault / "91_Canvas").glob("*.canvas")).read_text(encoding="utf-8"))
    for n in canvas["nodes"]:
        assert {"id", "type", "x", "y", "width", "height"} <= n.keys() and n["type"] in ("text", "file")
    assert all({"id", "fromNode", "toNode"} <= e.keys() for e in canvas["edges"])
    assert any(n.get("file") == "assets/snap/7.jpg" for n in canvas["nodes"])
    # 두 번째 실행: 바뀐 것 없음(결정적)
    rep2 = exo.export([(res, "a" * 64)], vault)["report"]
    assert "written" not in rep2 and "copied" not in rep2
    # 사람이 쓴 노트·UI로 고친 Base는 건드리지 않는다
    human = trend_files[0]
    human.write_text("---\ntype: trend\n---\n사람이 씀\n", encoding="utf-8")
    base = vault / exo.BASE_PATH
    base.write_text(base.read_text(encoding="utf-8") + "    image: note.image\n", encoding="utf-8")
    rep3 = exo.export([(res, "a" * 64)], vault)["report"]
    assert human.read_text(encoding="utf-8").endswith("사람이 씀\n") and "image: note.image" in base.read_text(encoding="utf-8")
    assert set(rep3["skipped_paths"]) == {str(human.relative_to(vault)), exo.BASE_PATH}


def test_slugify_and_frontmatter():
    assert exo.slugify('a:b|c#d^e[[f]]/g\\h') == "a b c d e f g h"
    fm = exo.frontmatter({"type": "product", "score": 0.5, "has_image": True, "trends": ["[[10_Trends/x]]"], "empty": None})
    assert 'score: 0.5' in fm and "has_image: true" in fm and '  - "[[10_Trends/x]]"' in fm and "empty" not in fm


def test_trend_summary_from_term_mentions(tmp_path):
    arts = tmp_path / "a.jsonl"
    arts.write_text(json.dumps({"article_id": "100", "title": "올가을 ‘테스트 코어’", "url": "https://v/a", "published_at": "2026-10-01T00:00:00+00:00",
                                "blocks": [{"idx": 0, "type": "paragraph", "text": "요즘 테스트 코어가 뜬다. 다른 문장."}]},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    tm = tmp_path / "tm.jsonl"
    tm.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"term": "테스트 코어", "norm": "테스트코어", "article_count": 1, "mention_count": 2, "style_article_count": 1,
         "months": {"2026-10": 1}, "seasons": {"2027SS": 1},
         "evidence": [{"article_id": "100", "block_idx": 0, "role": "mention", "published": "2026-10-01"},
                      {"article_id": "100", "block_idx": None, "role": "title", "published": "2026-10-01"}]},
        {"term": "드뮤어", "norm": "드뮤어", "article_count": 0, "evidence": []}]) + "\n", encoding="utf-8")
    articles = rts.load_articles(arts)
    rows = rts.build_rows(rts.rows_from_term_mentions(tm), articles, {"테스트코어": {"tier": 1, "match_mode": "graph"}})
    assert [r["term"] for r in rows] == ["테스트 코어", "드뮤어"]
    assert rows[0]["top_evidence"][0]["role"] == "title" and len(rows[0]["top_evidence"]) == 1  # 기사당 1개, title 우선
    md = rts.render_md(rows, "term_mentions", "f" * 64)
    assert "| 테스트 코어 | 1 | 2 | 2026-10 ×1 | 2027SS ×1 | graph (Tier 1) |" in md and "코퍼스에 없음" in md
    assert "generated: true" in md


def test_image_content_sidecar_orders_display_only(env):
    """L0 #203: content.parquet는 표시층에서만 — 묶음 순서 garment → uncertain/None → non_garment → 깨짐, 점수·rank 불변."""
    tmp, shared, res = env
    it = res.trends[0].items[0]
    a, b, snap = it.similar_products  # a: 02 ok · b: 02 구성품 broken · snap: 03 ok(판정 없음)
    content = {"otpensource:f.json:1": "non_garment", "otpensource:f.json:2": "garment"}
    assert rtr.image_content(b, content) == "garment"  # 구성품은 부모 판정
    assert [rtr.display_class(p, content) for p in (a, b, snap)] == [2, 3, 1]
    order = rtr.order_for_display(it.similar_products, content)
    assert [i for i, _ in order] == [3, 1, 2] and a.score == 0.9  # 표시만 바뀌고 원래 순번·점수는 그대로
    out = tmp / "results"
    out.mkdir()
    page = rtr.render(res, "0" * 64, out, content=content)
    assert 'src="http://cdn/a.jpg"' not in page and 'href="http://cdn/a.jpg"' in page  # non_garment = 자리표시 + 원본 링크
    assert "옷이 아닌 이미지일 수 있음" in page and "점수 순위 1" in page
    vault = tmp / "vault"
    exo.export([(res, "a" * 64)], vault, content=content)
    note = next(p for p in (vault / "40_Products").glob("A B*.md")).read_text(encoding="utf-8")
    assert "\nimage:" not in note and 'image_content: "non_garment"' in note and "image_order: 2" in note
    assert "[원본 이미지 보기](http://cdn/a.jpg)" in note
    item = next((vault / "20_Items").glob("*.md")).read_text(encoding="utf-8").split("## 비슷한 상품")[1]
    assert item.index("snap-000007") < item.index("A B")  # 정상·판정 없음(03)이 non_garment보다 먼저
