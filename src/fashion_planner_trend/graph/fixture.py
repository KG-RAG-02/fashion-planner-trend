"""합성 fixture 그래프 — Neo4j 테스트와 stub 프로필(오프라인 결정적 실행)이 같이 쓴다. Vogue 원문·상품 데이터는 없다.

- 테스트 데이터 `ev`·`synthetic_label`·`synthetic_crawl`·`product`·`synthetic_products`는 tests/conftest.py에서 옮겼고
  내용은 같다 (conftest가 다시 내보낸다).
- 테스트 그래프 = `test_sources()` + `synthetic_products()` (tests/test_graph_neo4j.py의 `g`).
- stub 그래프 = 테스트 그래프 + 기사 900003 "발레코어"(`stub_label`) + 블라우스 상품 2개(`stub_products`).
  900003은 Project_08 오프라인 질의 fixture("2027년 봄 발레코어에서 유행하는 핑크색 상의를 기획하고 싶어")가
  보내는 TrendQuery(`STUB_QUERY`)에 status ok · trends · evidence가 나오도록 맞췄다.
- 적재 순서는 테스트와 같다(`write_sources` → `load_sources`). `seed_stub_graph`는 비어 있지 않은 그래프를
  `allow_wipe` 없이 거부한다. 실행 진입점: scripts/seed_fixture_graph.py.
"""
from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from fashion_planner_trend.schemas.graph_schema import AttrEdge, DetailEdge, ProductNode, ProductRecord


# ---------- 합성 데이터 (Vogue 원문·상품 데이터 없이). tests/conftest.py에서 옮김 ----------
def ev(b: int, q: str) -> dict[str, Any]:
    return {"block_idx": b, "quote": q}


def synthetic_label(article_id: str = "900001", status: str = "silver", aliases: tuple[str, ...] = ("콰이어트 럭셔리",),
                    color_raw: str = "베이지", seasons: tuple[tuple[int, str], ...] = ((2027, "SS"),)) -> dict[str, Any]:
    """라벨(또는 status=auto 자동 추출)과 같은 형식의 기사 1건: Style 1 (Item 2: 상의 셔츠, 하의 스커트)."""
    return {
        "label_meta": {"article_id": article_id, "title": "합성 기사", "url": f"https://example.invalid/{article_id}",
                       "published": "2026-10-01", "status": status,
                       **({"labeler": "llm:openai_api/test-model", "prompt_version": "extract-test", "extracted_at": "2026-11-08T21:00:00+09:00"}
                          if status == "auto" else {})},
        "extraction": {"article_id": article_id, "styles": [{
            "name": "테스트 럭셔리", "name_origin": "article", "aliases": list(aliases),
            "seasons": [{"year": y, "season": s} for y, s in seasons],
            "definition": "합성 정의", "description": [{"text": "설명 하나", "evidence": [ev(1, "설명 인용")]}],
            "focus_attributes": [{"attribute": "color", "level": "core", "evidence": [ev(2, "색 인용")]}],
            "evidence": [ev(0, "이름 인용")],
            "items": [
                {"name": "베이지 셔츠", "raw_text": "베이지 셔츠", "category_minor": "셔츠",
                 "colors": [{"raw": color_raw, "value": "베이지" if color_raw == "베이지" else "그레이"}],
                 "materials": [{"raw": "면", "value": "면"}], "evidence": [ev(3, "셔츠 인용")],
                 "image_links": [{"image_idx": 0, "basis": "adjacent"}, {"image_idx": 9, "basis": "order"}]},
                {"name": "플리츠 스커트", "raw_text": "플리츠 스커트", "category_minor": "스커트",
                 "details": [{"raw": "플리츠", "type": "detail", "value": "플리츠"}], "evidence": [ev(4, "스커트 인용")]},
            ],
        }]},
    }


def synthetic_crawl(article_id: str = "900001") -> dict[str, Any]:
    return {"article_id": article_id, "title": "합성 기사", "url": f"https://example.invalid/{article_id}",
            "published_at": "2026-10-01T00:00:00+00:00",
            "blocks": [{"type": "paragraph", "idx": i, "text": f"블록 {i}"} for i in range(6)],
            "images": [{"image_idx": 0, "url": "https://img.example.invalid/0.jpg", "block_idx": 2},
                       {"image_idx": 1, "url": "https://img.example.invalid/1.jpg", "block_idx": 5}]}


def product(pid: str, major: str, minor: str | None = None, colors=(), materials=(), details=(), fit=None,
            source: str = "otpensource", **node: Any) -> ProductRecord:
    kind = "snap_garment" if source == "musinsa_snap" else "catalog_item"
    return ProductRecord(
        node=ProductNode(id=pid, name=pid, source=source, kind=kind, category_minor=minor, fit=fit, source_row=pid, **node),
        category_major=major, colors=[AttrEdge(value=c) for c in colors], materials=[AttrEdge(value=m) for m in materials],
        details=[DetailEdge(type=t, value=v) for t, v in details])


def synthetic_products() -> list[ProductRecord]:
    return [
        product("otpensource:a", "상의", "셔츠", colors=["베이지"], materials=["면"], image_url="https://x/a.jpg", image_status="ok"),
        product("otpensource:b", "상의", "셔츠", colors=["화이트"], image_url="https://x/b.jpg", image_status="broken"),
        product("otpensource:c", "상의", "티셔츠", colors=["베이지"], image_url="https://x/c.jpg"),
        product("musinsa_snap:1", "상의", "셔츠", materials=["면"], source="musinsa_snap", snap_id=1, image_ref="hf://x#row=1",
                image_status="ok", image_scope="full_snap"),
        product("musinsa_snap:2", "하의", "스커트", details=[("detail", "플리츠")], source="musinsa_snap", snap_id=1,
                image_ref="hf://x#row=1", image_status="ok", image_scope="full_snap"),
        product("otpensource:d", "하의", "스커트", colors=["블랙"]),
        product("otpensource:e", "하의", "팬츠", details=[("detail", "플리츠")]),
    ]


# ---------- stub 그래프 ----------
CONTRACT_VERSION = "vogue-handoff/0.1"
STUB_ARTICLE_ID = "900003"
STUB_REQUEST_ID = "stub-trend-001"
# Project_08 `VOGUE_QUERY_STRUCTURING_MODE=synthetic_fixture`가 위 질의로 만드는 TrendQuery와 같다 (request_id만 고정)
STUB_QUERY: dict[str, Any] = {
    "request_id": STUB_REQUEST_ID,
    "original_query": "2027년 봄 발레코어에서 유행하는 핑크색 상의를 기획하고 싶어",
    "trend_terms": {"season": {"year": 2027, "season": "SS", "raw": "2027년 봄", "origin": "user_requested"},
                    "style_names": [{"raw": "발레코어", "value": None}], "item_terms": []},
    "requested": {"categories": {"include": [{"raw": "상의", "major": "상의", "minor": None}], "exclude": []},
                  "gender": None, "attributes": [{"type": "color", "raw": "핑크색", "value": "핑크"}]},
    "unsupported": [],
    "missing_fields": [],
}
EVIDENCE_ID_RE = re.compile(r"^trend:[^:]+:\d+:[0-9a-f]{8}$")


def stub_request() -> dict[str, Any]:
    """examples/stub/trend-request.json: `vogue-handoff/0.1` envelope + `STUB_QUERY`."""
    return {"contract_version": CONTRACT_VERSION, "request_id": STUB_REQUEST_ID, "input": copy.deepcopy(STUB_QUERY)}


def stub_label(article_id: str = STUB_ARTICLE_ID) -> dict[str, Any]:
    """합성 라벨 1건: Style "발레코어"(2027 SS) — Item 핑크 블라우스(상의, 사진 연결) · 화이트 튤 스커트(하의)."""
    return {
        "label_meta": {"article_id": article_id, "title": "합성 기사 (발레코어)", "url": f"https://example.invalid/{article_id}",
                       "published": "2026-10-01", "status": "silver"},
        "extraction": {"article_id": article_id, "styles": [{
            "name": "발레코어", "name_origin": "article", "aliases": ["발레 코어"],
            "seasons": [{"year": 2027, "season": "SS"}],
            "definition": "합성 정의: 발레복에서 온 가볍고 여린 실루엣",
            "description": [{"text": "합성 설명: 시폰 블라우스와 튤 스커트", "evidence": [ev(1, "발레코어 설명 인용")]}],
            "focus_attributes": [{"attribute": "color", "level": "core", "evidence": [ev(2, "발레코어 색 인용")]}],
            "evidence": [ev(0, "발레코어 이름 인용")],
            "items": [
                {"name": "핑크 블라우스", "raw_text": "핑크 블라우스", "category_minor": "블라우스",
                 "colors": [{"raw": "핑크", "value": "핑크"}], "materials": [{"raw": "시폰", "value": "시폰"}],
                 "evidence": [ev(3, "블라우스 인용")], "image_links": [{"image_idx": 0, "basis": "adjacent"}]},
                {"name": "화이트 튤 스커트", "raw_text": "화이트 튤 스커트", "category_minor": "스커트",
                 "colors": [{"raw": "화이트", "value": "화이트"}], "evidence": [ev(4, "스커트 인용")]},
            ],
        }]},
    }


def stub_products() -> list[ProductRecord]:
    """테스트 상품 + 블라우스 2개 (핑크는 요청 색을 충족, 화이트는 '다른 값'으로 제외되는 경우)."""
    return synthetic_products() + [
        product("otpensource:p1", "상의", "블라우스", colors=["핑크"], materials=["시폰"], image_url="https://x/p1.jpg",
                image_status="ok"),
        product("otpensource:p2", "상의", "블라우스", colors=["화이트"], image_url="https://x/p2.jpg"),
    ]


def test_sources() -> list[tuple[str, str, dict[str, Any]]]:
    """(폴더, 기사 id, 문서): tests/test_graph_neo4j.py의 테스트 그래프. 900001은 라벨과 자동 추출이 모두 있고 라벨이 이긴다."""
    return [("labels", "900001", synthetic_label()),
            ("ext", "900001", synthetic_label(status="auto")),
            ("ext", "900002", synthetic_label("900002", status="auto", color_raw="차콜 그레이", aliases=(), seasons=()))]


test_sources.__test__ = False  # pytest가 이 함수를 테스트로 모으지 않게


def stub_sources() -> list[tuple[str, str, dict[str, Any]]]:
    return test_sources() + [("labels", STUB_ARTICLE_ID, stub_label())]


def write_sources(root: Path, sources: Optional[list[tuple[str, str, dict[str, Any]]]] = None) -> Path:
    """root 아래에 labels/·ext/·crawl.jsonl을 쓴다 (테스트와 같은 배치). 기본은 stub 그래프."""
    sources = stub_sources() if sources is None else sources
    root = Path(root)
    ids: list[str] = []
    for kind, aid, doc in sources:
        (root / kind).mkdir(parents=True, exist_ok=True)
        (root / kind / f"{aid}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        if aid not in ids:
            ids.append(aid)
    for kind in ("labels", "ext"):
        (root / kind).mkdir(parents=True, exist_ok=True)
    (root / "crawl.jsonl").write_text("\n".join(json.dumps(synthetic_crawl(a), ensure_ascii=False) for a in ids),
                                      encoding="utf-8")
    return root


def load_sources(driver: Any, root: Path, products: Iterable[ProductRecord]) -> tuple[set[str], dict[str, Any]]:
    """제약·인덱스 → 표준 값 → Product → 기사(라벨 우선). 반환: (적재한 Product id, 기사별 통계)."""
    from fashion_planner_trend.graph import loader

    loader.ensure_schema(driver)
    loader.seed_vocabulary(driver)
    seen: set[str] = set()
    loader.load_products(driver, products, seen=seen)
    stats = loader.load_articles(driver, Path(root) / "labels", Path(root) / "ext", Path(root) / "crawl.jsonl", prefer="labels")
    return seen, stats


class GraphNotEmpty(RuntimeError):
    def __init__(self, count: int):
        super().__init__(f"graph is not empty ({count} nodes)")
        self.count = count


def node_count(driver: Any) -> int:
    return int(driver.execute_query("MATCH (n) RETURN count(n) AS c").records[0]["c"])


def is_managed_cloud(uri: str) -> bool:
    """Neo4j Aura 같은 관리형 주소인가. stub 적재는 이런 그래프를 절대 비우지 않는다."""
    host = (urlsplit(uri).hostname or "").lower()
    return host == "neo4j.io" or host.endswith(".neo4j.io")


def seed_stub_graph(driver: Any, *, allow_wipe: bool = False) -> dict[str, Any]:
    """stub 그래프를 적재한다. 노드가 있으면 `allow_wipe` 없이는 GraphNotEmpty, 있으면 모두 지우고 적재한다."""
    from fashion_planner_trend.graph import loader

    existing = node_count(driver)
    if existing and not allow_wipe:
        raise GraphNotEmpty(existing)
    if existing:
        driver.execute_query("MATCH (n) DETACH DELETE n")
    with tempfile.TemporaryDirectory(prefix="trend-stub-graph-") as tmp:
        _, stats = load_sources(driver, write_sources(Path(tmp)), stub_products())
    return {"wiped_nodes": existing, "articles": stats, "counts": loader.graph_counts(driver)}


def run_stub_query(driver: Any) -> dict[str, Any]:
    """`STUB_QUERY`로 검색한 TrendAnalysisResult(JSON) — API `/runs`의 result와 같은 값."""
    from fashion_planner_trend.graph.search import TrendSearcher
    from fashion_planner_trend.schemas.trend_query import TrendQuery

    return TrendSearcher(driver).search(TrendQuery.model_validate(STUB_QUERY)).model_dump(mode="json")


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    """TrendAnalysisResult JSON → 비교용 요약 (examples/stub/trend-response-summary.json의 result)."""
    return {
        "status": result["status"],
        "schema_version": result.get("schema_version"),
        "trends": [{
            "style": {k: t["style"].get(k) for k in ("id", "name", "seasons", "article_id")},
            "example_images": t["style"].get("example_images", []),
            "items": [{"id": i["id"], "name": i["name"], "category_major": i.get("category_major"),
                       "category_minor": i.get("category_minor"), "colors": i.get("colors", []),
                       "image_urls": i.get("image_urls", []), "evidence_ids": i.get("evidence_ids", []),
                       "similar_product_ids": [p["product_id"] for p in i.get("similar_products", [])]}
                      for i in t.get("items", [])],
        } for t in result.get("trends", [])],
        "evidence_ids": [e["evidence_id"] for e in result.get("evidence", [])],
        "limitations": result.get("limitations", []),
    }
