"""크롤러: 날짜 하한·이미 받은 기사 건너뛰기·합친 풀(기존 줄 그대로)·robots.txt 준수. 네트워크 없이 합성 입력으로 확인한다."""
import json
from urllib.robotparser import RobotFileParser

import pytest
import requests

from fashion_planner_trend.ingestion.crawler import vogue_kr as V

REAL_ROBOTS_PARSER = V.robots_parser  # autouse fixture가 바꾸기 전의 실제 함수


def _robots(*lines):
    rp = RobotFileParser(V.ROBOTS_URL)
    rp.parse(list(lines))
    return rp


@pytest.fixture(autouse=True)
def _no_network_robots(monkeypatch):
    """기본은 모두 허용하는 robots.txt. 실제 사이트에는 요청하지 않는다."""
    monkeypatch.setattr(V, "robots_parser", lambda: _robots("User-agent: *", "Allow: /"))


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def _pages(pages):
    def fake_request(method, url, retries=V.RETRIES, **kw):
        n = kw["data"]["paged"]
        return FakeResp({"current_posts": pages[n - 1] if n <= len(pages) else []})
    return fake_request


def post(pid, date):
    return {"post_id": str(pid), "post_date": date, "permalink": f"/x/{pid}/", "post_title": f"t{pid}"}


def test_since_stops_after_two_old_pages_and_keeps_slightly_unordered(monkeypatch):
    pages = [[post(1, "2026.11.10"), post(2, "2026.01.02")],
             [post(3, "2025.12.30"), post(4, "2026.01.01")],   # 섞인 순서: 한 개는 하한 이상
             [post(5, "2025.12.20")], [post(6, "2025.12.10")],  # 두 페이지 연속 전부 오래됨 → 멈춤
             [post(7, "2026.02.01")]]                           # 보지 않는다
    monkeypatch.setattr(V, "_request", _pages(pages))
    monkeypatch.setattr(V.time, "sleep", lambda s: None)
    got = V.fetch_article_list(None, 0, since="2026-01-01")
    assert [p["post_id"] for p in got] == ["1", "2", "4"]


def test_crawl_skips_existing_ids(monkeypatch):
    monkeypatch.setattr(V, "fetch_article_list", lambda limit, delay, since=None, robots=None: [post(1, "2026.11.10"), post(2, "2026.11.09")])
    fetched = []

    def fake_get(url, **kw):
        fetched.append(url)
        raise RuntimeError("no network in test")
    monkeypatch.setattr(V, "_get", fake_get)
    monkeypatch.setattr(V.time, "sleep", lambda s: None)
    recs = V.crawl(None, 0, since="2026-01-01", skip_ids={"1"})
    assert len(recs) == 1 and fetched == ["https://www.vogue.co.kr/x/2/"] and recs[0]["error"]


def test_merge_pools_copies_existing_bytes_and_adds_only_good_new(tmp_path):
    old = tmp_path / "old.jsonl"
    line = '{"article_id": "100", "title": "기존", "blocks": [1, 2]}\n'
    old.write_bytes(line.encode("utf-8"))
    out = tmp_path / "merged.jsonl"
    stats = V.merge_pools([str(old)], [{"article_id": "200", "title": "새"}, {"article_id": "100", "title": "중복"},
                                       {"url": "u", "error": "boom"}], str(out))
    assert stats == {"kept": 1, "added": 1}
    lines = out.read_bytes().splitlines(keepends=True)
    assert lines[0] == line.encode("utf-8") and json.loads(lines[1])["article_id"] == "200"


def test_published_since_uses_publish_date():
    recs = [{"article_id": "1", "published_at": "2025-12-29T00:00:00+00:00"},   # 목록 날짜는 2026이어도 발행은 2025 → 뺌
            {"article_id": "2", "published_at": "2026-01-01T09:00:00+00:00"},
            {"url": "u", "error": "boom"}]
    keep, out = V.published_since(recs, "2026-01-01")
    assert [r.get("article_id") for r in out] == ["1"] and len(keep) == 2
    assert V.published_since(recs, None) == (recs, [])


def test_existing_ids_ignores_error_records(tmp_path):
    p = tmp_path / "pool.jsonl"
    p.write_text('{"article_id": "1"}\n{"url": "u", "error": "x"}\n\n', encoding="utf-8")
    assert V.existing_ids([str(p)]) == {"1"}


@pytest.mark.parametrize("raw,expected", [("2026.11.06", "2026-11-06"), ("", ""), (None, "")])
def test_list_date(raw, expected):
    assert V.list_date({"post_date": raw}) == expected


def test_user_agent_identifies_the_project_not_a_browser():
    assert V.HEADERS["User-Agent"] == V.USER_AGENT
    assert V.USER_AGENT.startswith("fashion-planner-trend/") and V.PROJECT_URL in V.USER_AGENT
    assert "Mozilla" not in V.USER_AGENT and "Chrome" not in V.USER_AGENT


def test_robots_disallow_stops_before_any_request(monkeypatch):
    calls = []
    monkeypatch.setattr(V, "_request", lambda *a, **kw: calls.append(a) or FakeResp({"current_posts": []}))
    with pytest.raises(V.RobotsDisallowed):
        V.fetch_article_list(None, 0, robots=_robots("User-agent: *", "Disallow: /wp-admin/"))
    assert calls == []


def test_robots_disallowed_article_stops_crawl(monkeypatch):
    monkeypatch.setattr(V, "fetch_article_list", lambda limit, delay, since=None, robots=None: [post(1, "2026.11.10")])
    monkeypatch.setattr(V, "_get", lambda url, **kw: pytest.fail("금지된 주소를 요청했다"))
    with pytest.raises(V.RobotsDisallowed):
        V.crawl(None, 0, robots=_robots("User-agent: fashion-planner-trend", "Disallow: /x/"))


class _Status:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


@pytest.mark.parametrize("response,allowed", [
    (_Status(200, "User-agent: *\nDisallow: /private/"), True),   # 규칙에 걸리지 않는 주소
    (_Status(404), True),                                          # robots.txt 없음 → 모두 허용
    (_Status(503), False),                                         # 서버 오류 → 확인 불가, 모두 금지
    (requests.ConnectionError("offline"), False),                  # 네트워크 오류 → 모두 금지
])
def test_robots_parser_status_handling(monkeypatch, response, allowed):
    def fake_get(url, **kw):
        assert url == V.ROBOTS_URL and kw["headers"]["User-Agent"] == V.USER_AGENT
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(V.requests, "get", fake_get)
    rp = REAL_ROBOTS_PARSER()
    assert rp.can_fetch(V.USER_AGENT, f"{V.BASE_URL}/x/1/") is allowed
