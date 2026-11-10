"""Loopback HTTP API for the Trend Analysis Agent's v0.1 handoff route."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from neo4j import GraphDatabase  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from trend_agent.config import neo4j_settings  # noqa: E402
from trend_agent.graph.search import TrendSearcher  # noqa: E402
from trend_agent.schemas.trend_analysis_result import SCHEMA_VERSION  # noqa: E402
from trend_agent.schemas.trend_query import TrendQuery  # noqa: E402

CONTRACT_VERSION = "vogue-handoff/0.1"
AGENT_ID = "trend-analysis-agent"
ROUTE = f"/v1/agents/{AGENT_ID}/runs"
MAX_BODY_BYTES = 1_000_000


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _validate_request(value: Any) -> tuple[str, TrendQuery]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_request", "Request body must be a JSON object.")
    if value.get("contract_version") != CONTRACT_VERSION:
        raise ApiError(400, "unsupported_contract_version", "Unsupported contract_version.")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ApiError(400, "invalid_request_id", "request_id must be a non-empty string of at most 128 characters.")
    input_value = value.get("input")
    if not isinstance(input_value, dict):
        raise ApiError(400, "invalid_input", "input must be a TrendQuery object.")
    if input_value.get("request_id") != request_id:
        raise ApiError(400, "request_id_mismatch", "input.request_id must match request_id.")
    try:
        query = TrendQuery.model_validate(input_value)
    except ValidationError as exc:
        # Pydantic errors may include user-provided values; return field locations only.
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise ApiError(400, "invalid_trend_query", "input failed TrendQuery validation: " + ", ".join(fields)) from None
    return request_id, query


def build_handler(searcher: TrendSearcher):
    class TrendHandler(BaseHTTPRequestHandler):
        server_version = "TrendAnalysisAgent/0.1"

        def log_message(self, fmt: str, *args: object) -> None:
            # Keep query bodies, headers, and environment configuration out of logs.
            return

        def _send_json(self, status: int, body: Mapping[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if urlparse(self.path).path == "/health":
                self._send_json(200, {"status": "ok", "agent_id": AGENT_ID})
            else:
                self._send_json(404, {"error": {"code": "not_found", "message": "Route not found."}})

        def _authorized(self) -> bool:
            expected = os.environ.get("VOGUE_AGENT_SERVICE_BEARER_TOKEN", "")
            authorization = self.headers.get("Authorization", "")
            supplied = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
            return bool(expected and supplied) and hmac.compare_digest(supplied, expected)

        def do_POST(self) -> None:
            if urlparse(self.path).path != ROUTE:
                self._send_json(404, {"error": {"code": "not_found", "message": "Route not found."}})
                return
            if not self._authorized():
                self._send_json(401, {"error": {"code": "unauthorized", "message": "Bearer authentication required."}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ApiError(400, "invalid_request", "Request body size is invalid.")
                try:
                    body = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ApiError(400, "invalid_json", "Request body must be valid JSON.") from None
                request_id, query = _validate_request(body)
            except ApiError as exc:
                self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
                return

            try:
                result = searcher.search(query).model_dump(mode="json")
            except Exception:
                self._send_json(503, {"error": {"code": "search_unavailable", "message": "Trend search is temporarily unavailable."}})
                return
            if result.get("request_id") != request_id or result.get("schema_version") != SCHEMA_VERSION:
                self._send_json(500, {"error": {"code": "invalid_search_result", "message": "Trend search returned incompatible result metadata."}})
                return
            status = result.get("status")
            if status not in {"ok", "partial", "no_results", "error"}:
                self._send_json(500, {"error": {"code": "invalid_search_result", "message": "Trend search returned an unsupported status."}})
                return
            self._send_json(200, {
                "contract_version": CONTRACT_VERSION,
                "request_id": request_id,
                "producer": {"agent_id": AGENT_ID, "schema_version": SCHEMA_VERSION},
                "status": status,
                "result": result,
            })

    return TrendHandler


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.environ.get("TREND_API_HOST", "127.0.0.1")
    port = port if port is not None else int(os.environ.get("TREND_API_PORT", "8768"))
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Trend API may bind only to loopback addresses.")
    settings = neo4j_settings()
    driver = GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password))
    try:
        driver.verify_connectivity()
    except Exception:
        driver.close()
        raise SystemExit("Cannot connect to Neo4j; check the local Neo4j service and NEO4J_* settings.") from None
    server = ThreadingHTTPServer((host, port), build_handler(TrendSearcher(driver)))
    print(f"Trend API listening on http://{host}:{port}{ROUTE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="Loopback address only (default: TREND_API_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Listen port (default: TREND_API_PORT or 8768)")
    args = parser.parse_args()
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
