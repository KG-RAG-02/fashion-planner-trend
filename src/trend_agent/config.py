"""`.env` 설정 읽기. 값은 로그·예외 메시지에 넣지 않는다. 이름의 기준은 `.env.example`."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith(("NEO4J_", "LLM_"))})
    return values


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
