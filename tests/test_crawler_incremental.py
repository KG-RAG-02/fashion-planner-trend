"""D-45 크롤러 증분: 날짜 하한·이미 받은 기사 건너뛰기·합친 풀(기존 줄 그대로). 네트워크 없이 합성 목록으로 확인한다."""
import json

import pytest

from trend_agent.ingestion.crawler import vogue_kr as V


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
    monkeypatch.setattr(V, "fetch_article_list", lambda limit, delay, since=None: [post(1, "2026.11.10"), post(2, "2026.11.09")])
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
