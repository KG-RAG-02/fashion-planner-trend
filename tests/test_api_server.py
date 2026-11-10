from __future__ import annotations

import json
import sys
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.api.server import (  # noqa: E402
    CONTRACT_VERSION,
    ROUTE,
    ApiError,
    _validate_request,
    build_handler,
)
from trend_agent.schemas.trend_analysis_result import TrendAnalysisResult  # noqa: E402


def request_payload(request_id="api-test-001"):
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "input": {
            "request_id": request_id,
            "original_query": "2027 SS 시럽코어 상의",
            "trend_terms": {
                "season": {"year": 2027, "season": "SS", "raw": "2027 SS", "origin": "user_requested"},
                "style_names": [{"raw": "시럽코어", "value": None}],
                "item_terms": [],
            },
            "requested": {"categories": {"include": [{"raw": "상의", "major": "상의"}], "exclude": []}},
            "unsupported": [],
            "missing_fields": [],
        },
    }


def test_request_validates_as_trend_query():
    request_id, query = _validate_request(request_payload())
    assert request_id == query.request_id == "api-test-001"


@pytest.mark.parametrize("payload,code", [
    ({}, "unsupported_contract_version"),
    ({"contract_version": CONTRACT_VERSION, "request_id": "x", "input": {}}, "request_id_mismatch"),
    ({"contract_version": CONTRACT_VERSION, "request_id": "x", "input": {"request_id": "x"}}, "invalid_trend_query"),
])
def test_invalid_requests_are_rejected(payload, code):
    with pytest.raises(ApiError) as exc:
        _validate_request(payload)
    assert exc.value.code == code


class FakeSearcher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def search(self, query):
        if self.error:
            raise self.error
        return self.result


def call_api(searcher, body, path=ROUTE):
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(searcher))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = json.dumps(body).encode()
        req = Request(f"http://127.0.0.1:{server.server_address[1]}{path}", data=data,
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=2) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_success_response_preserves_result_and_envelope():
    from trend_agent.api.server import _validate_request
    _, query = _validate_request(request_payload())
    result = TrendAnalysisResult(request_id=query.request_id, status="ok", limitations=["synthetic test"])
    status, response = call_api(FakeSearcher(result), request_payload())
    assert status == 200
    assert response["contract_version"] == "vogue-handoff/0.1"
    assert response["request_id"] == result.request_id
    assert response["producer"] == {"agent_id": "trend-analysis-agent", "schema_version": "1.1"}
    assert response["status"] == response["result"]["status"] == "ok"
    assert response["result"] == result.model_dump(mode="json")


def test_no_results_is_a_normal_http_response():
    from trend_agent.api.server import _validate_request
    _, query = _validate_request(request_payload())
    result = TrendAnalysisResult(request_id=query.request_id, status="no_results", limitations=["no match"])
    status, response = call_api(FakeSearcher(result), request_payload())
    assert status == 200
    assert response["status"] == response["result"]["status"] == "no_results"


def test_search_failure_returns_safe_service_error():
    status, response = call_api(FakeSearcher(error=RuntimeError("secret database details")), request_payload())
    assert status == 503
    assert response["error"]["code"] == "search_unavailable"
    assert "secret" not in json.dumps(response)


def test_invalid_body_is_rejected_before_search():
    status, response = call_api(FakeSearcher(), {"bad": True})
    assert status == 400
    assert response["error"]["code"] == "unsupported_contract_version"
