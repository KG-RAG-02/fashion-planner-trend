"""Exercise real handler methods without TCP; live HTTP tests remain separate."""
from io import BytesIO
import json
import os
from unittest.mock import patch

import pytest

from fashion_planner_trend.api.server import ROUTE, build_handler
from fashion_planner_trend.config import load_env
from fashion_planner_trend.schemas.trend_analysis_result import TrendAnalysisResult
from test_api_server import request_payload


class Searcher:
    def __init__(self, status='no_results'):
        self.calls = 0
        self.status = status

    def search(self, query):
        self.calls += 1
        return TrendAnalysisResult(request_id=query.request_id, status=self.status,
                                   limitations=['synthetic; no live graph observation'])


def call(searcher, body=b'{}', token='synthetic', length=None, path=ROUTE, env=None):
    handler = object.__new__(build_handler(searcher))
    handler.path = path
    handler.headers = {'Content-Length': str(len(body)) if length is None else length}
    if token is not None:
        handler.headers['Authorization'] = f'Bearer {token}'
    handler.rfile = BytesIO(body)
    output = []
    handler._send_json = lambda status, value: output.append((status, value))
    with patch('fashion_planner_trend.api.server.load_env', return_value=env if env is not None else {'VOGUE_AGENT_SERVICE_BEARER_TOKEN': 'synthetic'}):
        handler.do_POST()
    return output[0], handler.rfile.tell()


@pytest.mark.parametrize('token', [None, '', 'wrong'])
def test_unauthenticated_request_does_not_read_body_or_search(token):
    searcher = Searcher()
    (status, _), read = call(searcher, token=token)
    assert status == 401 and read == 0 and searcher.calls == 0


@pytest.mark.parametrize('env', [{}, {'VOGUE_AGENT_SERVICE_BEARER_TOKEN': ''}])
def test_unconfigured_auth_fails_closed(env):
    searcher = Searcher()
    (status, _), read = call(searcher, env=env)
    assert status == 401 and read == 0 and searcher.calls == 0


@pytest.mark.parametrize('body,length,code', [
    (b'{}', None, 'unsupported_contract_version'),
    (b'{', None, 'invalid_json'),
    (b'\xff', None, 'invalid_json'),
    (b'{}', 'bad', 'invalid_request'),
    (b'{}', '-1', 'invalid_request'),
    (b'{}', '1000001', 'invalid_request'),
    (b'', '0', 'invalid_request'),
])
def test_authorized_malformed_is_rejected_before_search(body, length, code):
    searcher = Searcher()
    (status, value), _ = call(searcher, body, length=length)
    assert status == 400 and value['error']['code'] == code and searcher.calls == 0


def test_env_only_token_authorizes_malformed_without_inference(tmp_path):
    with patch.dict(os.environ, {'VOGUE_AGENT_SERVICE_BEARER_TOKEN': 'synthetic'}, clear=True):
        env = load_env(tmp_path / 'absent.env')
    searcher = Searcher()
    (status, _), _ = call(searcher, env=env)
    assert status == 400 and searcher.calls == 0


@pytest.mark.parametrize('status', ['no_results', 'error'])
def test_producer_status_limitations_schema_and_id_preserved(status):
    searcher = Searcher(status)
    payload = request_payload()
    (http_status, value), _ = call(searcher, json.dumps(payload).encode())
    assert http_status == 200 and searcher.calls == 1
    assert value['status'] == value['result']['status'] == status
    assert value['request_id'] == value['result']['request_id'] == payload['request_id']
    assert value['producer']['schema_version'] == value['result']['schema_version'] == '1.1'
    assert value['result']['limitations'] == ['synthetic; no live graph observation']
    assert value['result']['trends'] == []
    assert not any(k in value['result'] for k in ('stock', 'quantity', 'errors'))
