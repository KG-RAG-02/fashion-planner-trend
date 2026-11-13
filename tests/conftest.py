"""공용 픽스처. `neo4j` 마커 = Neo4j **테스트 인스턴스(7688)** 가 필요한 테스트 (pytest.ini는 계약 파일이라 여기서 등록한다).

실행:  NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=<로컬값> pytest -m neo4j
       (테스트 인스턴스: docker run -d --name trend-agent-neo4j-test -p 127.0.0.1:7688:7687 -e NEO4J_AUTH=neo4j/<로컬값> neo4j:5.26-community)
- `NEO4J_TEST_URI`가 없으면 neo4j 테스트는 skip한다. 로컬 주소가 아니거나(Aura 등) 7687·`NEO4J_URI`(운영 그래프)를 가리키면 실패시킨다 — 테스트는 그래프를 비운다 (D-35).
"""
from __future__ import annotations

import os

import pytest

# 합성 데이터 (Vogue 원문·상품 데이터 없이)는 stub 그래프 적재(scripts/seed_fixture_graph.py)와 같이 쓰려고
# trend_agent.graph.fixture로 옮겼다. 테스트는 계속 `from conftest import ...`로 쓴다.
from trend_agent.graph.fixture import (  # noqa: F401
    ev,
    product,
    synthetic_crawl,
    synthetic_label,
    synthetic_products,
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "neo4j: Neo4j 테스트 인스턴스(7688)가 필요하다. NEO4J_TEST_URI 없으면 skip")


# 테스트에서 가짜 백엔드와 함께 쓰는 모델 이름. 모델 등록표(configs/llm_models.yaml)는 실제 모델만 담으므로,
# 이 이름들은 프로세스 안에서만 임시 등록한다 (docs/pipeline.md §14.2 `register_model`). 등록표 파일은 바꾸지 않는다.
TEST_MODELS = {"m": "standard", "m-1": "standard", "m-test": "standard", "gpt-5-mini": "reasoning"}


@pytest.fixture(autouse=True)
def _test_llm_models():
    from trend_agent.llm import models
    for name, profile in TEST_MODELS.items():
        models.register_model(name, profile, note="test")
    yield
    for name in TEST_MODELS:
        models.unregister_model(name)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NEO4J_TEST_URI"):
        return
    skip = pytest.mark.skip(reason="NEO4J_TEST_URI 없음 (Neo4j 테스트 인스턴스 7688)")
    for item in items:
        if "neo4j" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def neo4j_test_driver():
    from urllib.parse import urlsplit

    from trend_agent.config import Neo4jSettings, neo4j_driver, neo4j_settings

    uri = os.environ["NEO4J_TEST_URI"]
    parts = urlsplit(uri)
    if parts.hostname not in ("localhost", "127.0.0.1"):
        pytest.fail("NEO4J_TEST_URI가 로컬 주소가 아니다(Aura 등) — 테스트는 그래프를 비우므로 로컬 7688 테스트 인스턴스만 쓴다 (D-35)")
    if (parts.port or 7687) == 7687 or uri.rstrip("/") == neo4j_settings().uri.rstrip("/"):
        pytest.fail("NEO4J_TEST_URI가 7687 또는 NEO4J_URI(운영 그래프)와 같다 — 테스트는 그래프를 비우므로 7688 테스트 인스턴스만 쓴다")
    driver = neo4j_driver(Neo4jSettings(uri, os.environ.get("NEO4J_TEST_USER", "neo4j"), os.environ.get("NEO4J_TEST_PASSWORD", "")))
    driver.verify_connectivity()
    yield driver
    driver.close()
