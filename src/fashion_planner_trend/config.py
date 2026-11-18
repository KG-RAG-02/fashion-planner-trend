"""`.env` 설정 읽기. 값은 로그·예외 메시지에 넣지 않는다. 이름의 기준은 `.env.example`."""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SERVICE_ENV_KEYS = frozenset({
    "FASHION_PLANNER_SERVICE_TOKEN", "TREND_API_HOST", "TREND_API_PORT", "TREND_API_MODE",
})


def load_env(path: Path | str | None = None) -> dict[str, str]:
    """`.env`를 읽어 dict로 낸다 (이미 설정된 환경변수가 우선). 파일이 없으면 환경변수만 쓴다."""
    values: dict[str, str] = {}
    p = Path(path) if path else ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    # An explicitly empty process value also overrides dotenv (e.g. to revoke a token).
    values.update({k: v for k, v in os.environ.items()
                   if k in values or k in SERVICE_ENV_KEYS or k.startswith(("NEO4J_", "LLM_"))})
    return values


@dataclass(frozen=True)
class ApiSettings:
    host: str = "127.0.0.1"
    port: int = 8768
    mode: str = "local"


def api_settings(env: dict[str, str] | None = None, *, host: str | None = None,
                 port: int | None = None, mode: str | None = None) -> ApiSettings:
    """CLI > process env > dotenv > defaults. Only explicit service mode permits wildcard bind.

    Empty host/port/mode use defaults. An empty token disables authentication and cannot
    start a service bind. Tokens never appear in the returned settings or errors.
    """
    env = env if env is not None else load_env()
    selected_host = host if host is not None else (env.get("TREND_API_HOST") or "127.0.0.1")
    selected_mode = mode if mode is not None else (env.get("TREND_API_MODE") or "local")
    raw_port = port if port is not None else (env.get("TREND_API_PORT") or "8768")
    if selected_mode not in {"local", "service"}:
        raise ValueError("TREND_API_MODE must be local or service.")
    allowed = {"127.0.0.1", "::1", "localhost"} if selected_mode == "local" else {"0.0.0.0"}
    if selected_host not in allowed:
        raise ValueError("Local mode requires loopback; service mode requires explicit 0.0.0.0 host.")
    try:
        if isinstance(raw_port, bool):
            raise ValueError
        selected_port = int(raw_port)
        if str(selected_port) != str(raw_port).strip() or not 1 <= selected_port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("TREND_API_PORT must be an integer between 1 and 65535.") from None
    if selected_mode == "service" and not env.get("FASHION_PLANNER_SERVICE_TOKEN", "").strip():
        raise ValueError("Service mode requires FASHION_PLANNER_SERVICE_TOKEN.")
    return ApiSettings(selected_host, selected_port, selected_mode)


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str
    user: str
    password: str = ""

    def __repr__(self) -> str:  # 비밀번호를 찍지 않는다
        return f"Neo4jSettings(uri={self.uri!r}, user={self.user!r}, password=***)"


def neo4j_settings(env: dict[str, str] | None = None) -> Neo4jSettings:
    env = env if env is not None else load_env()
    # `.env.example`를 복사한 빈 값("")도 설정하지 않은 것으로 보고 기본값을 쓴다
    return Neo4jSettings(
        uri=env.get("NEO4J_URI") or "bolt://localhost:7687",
        user=env.get("NEO4J_USER") or "neo4j",
        password=env.get("NEO4J_PASSWORD") or "",
    )


# 인증서를 검증하는 암호화 주소 → 같은 프로토콜의 일반 주소 (docs/troubleshooting.md T-NEO4J-01)
_VERIFIED_TLS_SCHEMES = {"neo4j+s": "neo4j", "bolt+s": "bolt"}


def neo4j_driver_args(settings: Neo4jSettings) -> tuple[str, dict[str, Any]]:
    """`GraphDatabase.driver`에 넘길 (주소, 추가 설정).

    `neo4j+s://`·`bolt+s://`(Aura)는 `neo4j://`·`bolt://` + `encrypted=True` + `ssl_context`로 바꾼다.
    `ssl_context`는 운영체제 인증서 저장소와 certifi 묶음을 함께 신뢰한다 — Windows 저장소에는 Aura 체인의
    루트(SSL.com)가 없을 수 있어 `+s` 주소 그대로는 `ServiceUnavailable`이 난다. 드라이버는 `+s` 주소에
    `ssl_context`를 같이 받지 않으므로 주소를 바꾼다. `+ssc`와 암호화 없는 주소는 그대로 둔다.
    """
    scheme, sep, rest = settings.uri.partition("://")
    plain = _VERIFIED_TLS_SCHEMES.get(scheme.lower()) if sep else None
    if plain is None:
        return settings.uri, {}
    ctx = ssl.create_default_context()
    try:
        import certifi
    except ImportError:  # certifi가 없으면 운영체제 저장소만 쓴다 (원래 `+s`와 같다)
        pass
    else:
        ctx.load_verify_locations(certifi.where())
    return f"{plain}://{rest}", {"encrypted": True, "ssl_context": ctx}


def neo4j_driver(settings: Neo4jSettings | None = None, **kwargs: Any):
    """Neo4j 드라이버를 연다. 접속 스크립트·테스트는 `GraphDatabase.driver` 대신 이것을 쓴다."""
    from neo4j import GraphDatabase

    s = settings or neo4j_settings()
    uri, extra = neo4j_driver_args(s)
    return GraphDatabase.driver(uri, auth=(s.user, s.password), **extra, **kwargs)
