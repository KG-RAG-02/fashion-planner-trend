"""D-48: Style 예시 사진(최대 5장) — Item 연결 → Style 단독 섹션 → Style 1개 기사 전체 순 (Neo4j 없이, 합성 기사)."""
import copy

from conftest import synthetic_crawl, synthetic_label
from fashion_planner_trend.graph import loader
from fashion_planner_trend.graph.search import TrendSearcher
from fashion_planner_trend.schemas.article_extraction import ArticleExtraction


def _ev(b):
    return {"block_idx": b, "quote": f"블록 {b}"}


def _img(i, section):
    return {"image_idx": i, "url": f"https://img.example.invalid/{i}.jpg", "section_idx": section}


def _two_style_article():
    """섹션 0(도입, 두 Style이 함께 씀) · 섹션 1(Style A) · 섹션 2(Style B)."""
    doc = synthetic_label()
    a = doc["extraction"]["styles"][0]
    b = copy.deepcopy(a)
    b["name"], b["aliases"] = "테스트 B", []
    a["evidence"], a["description"][0]["evidence"], a["focus_attributes"][0]["evidence"] = [_ev(0)], [_ev(2)], [_ev(2)]
    b["evidence"], b["description"][0]["evidence"], b["focus_attributes"][0]["evidence"] = [_ev(0)], [_ev(5)], [_ev(5)]
    for it in a["items"] + b["items"]:
        it["image_links"] = []
        it["evidence"] = []
    doc["extraction"]["styles"] = [a, b]
    crawl = synthetic_crawl()
    crawl["blocks"] = [{"type": "paragraph", "idx": i, "text": f"블록 {i}", "section_idx": s}
                       for i, s in enumerate([0, 0, 1, 1, 2, 2])]
    crawl["images"] = [_img(0, 0), _img(1, 1), _img(2, 1), _img(3, 2)]
    return ArticleExtraction.model_validate(doc["extraction"]), crawl


def test_single_style_item_link_then_article():
    ext = ArticleExtraction.model_validate(synthetic_label()["extraction"])
    got = loader.style_example_images(ext.styles, synthetic_crawl())
    # Item 연결 0번(9번은 크롤 맵에 없어 버림) → 기사 사진 전체(0번은 중복이라 1번만)
    assert got == [[("https://img.example.invalid/0.jpg", "item_link"), ("https://img.example.invalid/1.jpg", "article")]]


def test_multi_style_uses_only_own_sections():
    ext, crawl = _two_style_article()
    a, b = loader.style_example_images(ext.styles, crawl)
    # 섹션 0은 두 Style이 같이 써서 빠지고, Style이 2개라 기사 전체 규칙도 쓰지 않는다
    assert a == [("https://img.example.invalid/1.jpg", "section"), ("https://img.example.invalid/2.jpg", "section")]
    assert b == [("https://img.example.invalid/3.jpg", "section")]


def test_limit_and_no_crawl():
    ext = ArticleExtraction.model_validate(synthetic_label()["extraction"])
    crawl = synthetic_crawl()
    crawl["images"] = [_img(i, None) for i in range(8)]
    got = loader.style_example_images(ext.styles, crawl)[0]
    assert len(got) == loader.EXAMPLE_IMAGE_LIMIT and len({u for u, _ in got}) == 5
    assert loader.style_example_images(ext.styles, None) == [[]]


class _FakeDriver:
    def __init__(self):
        self.written = None

    def session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute_write(self, fn, article, styles):
        self.written = styles


def test_load_label_writes_style_props():
    d = _FakeDriver()
    loader.load_label(d, synthetic_label(), image_map=loader.image_map_of(synthetic_crawl()), crawl=synthetic_crawl())
    props = d.written[0]["props"]
    assert props["example_image_urls"] == ["https://img.example.invalid/0.jpg", "https://img.example.invalid/1.jpg"]
    assert props["example_image_basis"] == ["item_link", "article"]


def test_trend_style_output():
    s = {"id": "900001-s0", "name": "테스트", "name_origin": "article", "definition": "정의",
         "example_image_urls": ["https://img.example.invalid/0.jpg"], "example_image_basis": ["section"]}
    out = TrendSearcher.trend_style(s, [])
    assert [(x.url, x.basis) for x in out.example_images] == [("https://img.example.invalid/0.jpg", "section")]
    old = {k: v for k, v in s.items() if not k.startswith("example_")}  # 예시 사진이 없는 기존 Style 노드
    assert TrendSearcher.trend_style(old, []).example_images == []
