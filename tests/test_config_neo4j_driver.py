"""`config.neo4j_driver_args`: Aura(`+s`) 주소는 인증서 묶음을 더한 ssl_context로 바꿔 연다 (pipeline.md D-35 ⑦). Neo4j 없이 돈다."""
from __future__ import annotations

import ssl

import pytest

from trend_agent.config import Neo4jSettings, neo4j_driver_args


@pytest.mark.parametrize("uri, expected", [
    ("neo4j+s://abc.databases.neo4j.io", "neo4j://abc.databases.neo4j.io"),
    ("bolt+s://abc.databases.neo4j.io:7687", "bolt://abc.databases.neo4j.io:7687"),
    ("NEO4J+S://abc.databases.neo4j.io", "neo4j://abc.databases.neo4j.io"),
])
def test_verified_tls_uri_uses_ssl_context(uri: str, expected: str) -> None:
    out, extra = neo4j_driver_args(Neo4jSettings(uri, "u", "p"))
    assert out == expected
    assert extra["encrypted"] is True
    ctx = extra["ssl_context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.mark.parametrize("uri", ["bolt://localhost:7687", "neo4j://localhost:7687", "neo4j+ssc://abc.databases.neo4j.io"])
def test_other_uris_unchanged(uri: str) -> None:
    assert neo4j_driver_args(Neo4jSettings(uri, "u", "p")) == (uri, {})


def test_driver_accepts_args() -> None:
    """드라이버가 바꾼 주소 + encrypted + ssl_context 조합을 받는지 (접속은 하지 않는다)."""
    from trend_agent.config import neo4j_driver

    d = neo4j_driver(Neo4jSettings("neo4j+s://abc.databases.neo4j.io", "u", "p"))
    try:
        assert d.encrypted
    finally:
        d.close()
