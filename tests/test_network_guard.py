"""테스트 중 외부 네트워크 차단 장치(conftest)가 동작하는지 확인한다. 실제 요청은 나가지 않는다."""
import socket

import pytest
import requests

from conftest import ExternalNetworkBlocked
from fashion_planner_trend.ingestion.crawler import vogue_kr

pytestmark = pytest.mark.expects_network_block


def test_external_dns_and_connect_are_blocked():
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(ExternalNetworkBlocked):
        socket.create_connection(("93.184.216.34", 443), timeout=1)


def test_requests_to_the_article_site_are_blocked_before_leaving():
    with pytest.raises((ExternalNetworkBlocked, requests.ConnectionError)):
        requests.get(vogue_kr.ROBOTS_URL, timeout=1)


def test_unpatched_robots_parser_cannot_reach_the_site():
    """대역 없이 robots_parser()를 불러도 이름 조회 단계에서 막혀 요청이 나가지 않는다."""
    with pytest.raises(ExternalNetworkBlocked):
        vogue_kr.robots_parser()


def test_loopback_is_allowed():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        socket.create_connection(srv.getsockname(), timeout=1).close()
    finally:
        srv.close()
