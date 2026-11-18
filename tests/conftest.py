"""공용 픽스처. `neo4j` 마커 = Neo4j **테스트 인스턴스(7688)** 가 필요한 테스트 (pytest.ini는 계약 파일이라 여기서 등록한다).

실행:  NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=<로컬값> pytest -m neo4j
       (테스트 인스턴스: docker run -d --name trend-agent-neo4j-test -p 127.0.0.1:7688:7687 -e NEO4J_AUTH=neo4j/<로컬값> neo4j:5.26-community)
- `NEO4J_TEST_URI`가 없으면 neo4j 테스트는 skip한다. 로컬 주소가 아니거나(Aura 등) 7687·`NEO4J_URI`(운영 그래프)를 가리키면 실패시킨다 — 테스트는 그래프를 비운다.
"""
from __future__ import annotations

import ipaddress
import os
import socket

import pytest

# 합성 데이터 (기사 원문·상품 데이터 없이)는 stub 그래프 적재(scripts/seed_fixture_graph.py)와 같이 쓰려고
# fashion_planner_trend.graph.fixture로 옮겼다. 테스트는 계속 `from conftest import ...`로 쓴다.
from fashion_planner_trend.graph.fixture import (  # noqa: F401
    ev,
    product,
    synthetic_crawl,
    synthetic_label,
    synthetic_products,
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "neo4j: Neo4j 테스트 인스턴스(7688)가 필요하다. NEO4J_TEST_URI 없으면 skip")
    config.addinivalue_line("markers", "expects_network_block: 외부 네트워크 차단이 일어나는 것을 확인하는 테스트")


# ---------- 외부 네트워크 차단 ----------
# 테스트는 오프라인으로만 돈다. 이 프로세스 안의 이름 조회와 연결은 루프백(로컬 HTTP 서버 테스트, 로컬 Neo4j
# 테스트 인스턴스)만 허용하고, 그 밖의 주소는 요청이 나가기 전에 실패시킨다. 대역을 빠뜨린 테스트가
# 실제 사이트(예: robots.txt)나 API에 닿지 않게 하는 안전장치다.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class ExternalNetworkBlocked(RuntimeError):
    pass


def _is_loopback(host: object) -> bool:
    if host is None:
        return True  # 주소 없이 bind하는 경우
    name = host.decode() if isinstance(host, bytes) else str(host)
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch, request):
    real_getaddrinfo, real_connect, real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
    attempts: list[str] = []

    def _block(what: str):
        attempts.append(what)
        raise ExternalNetworkBlocked(f"테스트 중 외부 {what} 차단")

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            _block(f"이름 조회: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
            _block(f"연결: {address[0]}")

    def connect(self, address):
        _check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    # 코드가 예외를 삼켜도(넓은 except) 외부 접근 시도가 있었으면 그 테스트를 실패로 본다.
    if attempts and request.node.get_closest_marker("expects_network_block") is None:
        pytest.fail("외부 네트워크 접근 시도: " + "; ".join(attempts))


# 테스트에서 가짜 백엔드와 함께 쓰는 모델 이름. 모델 등록표(configs/llm_models.yaml)는 실제 모델만 담으므로,
# 이 이름들은 프로세스 안에서만 임시 등록한다 (`register_model`). 등록표 파일은 바꾸지 않는다.
TEST_MODELS = {"m": "standard", "m-1": "standard", "m-test": "standard", "gpt-5-mini": "reasoning"}


@pytest.fixture(autouse=True)
def _test_llm_models():
    from fashion_planner_trend.llm import models
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

    from fashion_planner_trend.config import Neo4jSettings, neo4j_driver, neo4j_settings

    uri = os.environ["NEO4J_TEST_URI"]
    parts = urlsplit(uri)
    if parts.hostname not in ("localhost", "127.0.0.1"):
        pytest.fail("NEO4J_TEST_URI가 로컬 주소가 아니다(Aura 등) — 테스트는 그래프를 비우므로 로컬 7688 테스트 인스턴스만 쓴다")
    if (parts.port or 7687) == 7687 or uri.rstrip("/") == neo4j_settings().uri.rstrip("/"):
        pytest.fail("NEO4J_TEST_URI가 7687 또는 NEO4J_URI(운영 그래프)와 같다 — 테스트는 그래프를 비우므로 7688 테스트 인스턴스만 쓴다")
    driver = neo4j_driver(Neo4jSettings(uri, os.environ.get("NEO4J_TEST_USER", "neo4j"), os.environ.get("NEO4J_TEST_PASSWORD", "")))
    driver.verify_connectivity()
    yield driver
    driver.close()
