"""공용 픽스처. `neo4j` 마커 = Neo4j **테스트 인스턴스(7688)** 가 필요한 테스트 (pytest.ini는 계약 파일이라 여기서 등록한다).

실행:  NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=<로컬값> pytest -m neo4j
       (테스트 인스턴스: docker run -d --name trend-agent-neo4j-test -p 127.0.0.1:7688:7687 -e NEO4J_AUTH=neo4j/<로컬값> neo4j:5.26-community)
- `NEO4J_TEST_URI`가 없으면 neo4j 테스트는 skip한다. 7687(운영 그래프)을 가리키면 실패시킨다 — 테스트는 그래프를 비운다.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

from trend_agent.schemas.graph_schema import AttrEdge, DetailEdge, ProductNode, ProductRecord


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "neo4j: Neo4j 테스트 인스턴스(7688)가 필요하다. NEO4J_TEST_URI 없으면 skip")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NEO4J_TEST_URI"):
        return
    skip = pytest.mark.skip(reason="NEO4J_TEST_URI 없음 (Neo4j 테스트 인스턴스 7688)")
    for item in items:
        if "neo4j" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def neo4j_test_driver():
    from neo4j import GraphDatabase

    uri = os.environ["NEO4J_TEST_URI"]
    if uri.rstrip("/").endswith(":7687"):
        pytest.fail("NEO4J_TEST_URI가 7687(운영 그래프)이다 — 테스트는 그래프를 비우므로 7688 테스트 인스턴스만 쓴다")
    driver = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_TEST_USER", "neo4j"), os.environ.get("NEO4J_TEST_PASSWORD", "")))
    driver.verify_connectivity()
    yield driver
    driver.close()


# ---------- 합성 데이터 (Vogue 원문·상품 데이터 없이) ----------
def ev(b: int, q: str) -> dict[str, Any]:
    return {"block_idx": b, "quote": q}


def synthetic_label(article_id: str = "900001", status: str = "silver", aliases: tuple[str, ...] = ("콰이어트 럭셔리",),
                    color_raw: str = "베이지", seasons: tuple[tuple[int, str], ...] = ((2027, "SS"),)) -> dict[str, Any]:
    """라벨(또는 status=auto 자동 추출)과 같은 형식의 기사 1건: Style 1 (Item 2: 상의 셔츠, 하의 스커트)."""
    return {
        "label_meta": {"article_id": article_id, "title": "합성 기사", "url": f"https://example.invalid/{article_id}",
                       "published": "2026-10-01", "status": status,
                       **({"labeler": "llm:codex_cli/test-model", "prompt_version": "extract-test", "extracted_at": "2026-11-08T21:00:00+09:00"}
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
