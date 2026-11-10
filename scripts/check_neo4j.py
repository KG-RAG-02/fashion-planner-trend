"""Neo4j 접속 확인. `docker compose up -d` 뒤에 실행한다. 비밀번호는 출력하지 않는다.

실행:  python scripts/check_neo4j.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from trend_agent.config import neo4j_driver, neo4j_settings  # noqa: E402


def main() -> int:
    s = neo4j_settings()
    print("접속:", s)
    try:
        with neo4j_driver(s) as driver:
            driver.verify_connectivity()
            rec = driver.execute_query("CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS v, edition")
            name, v, edition = rec.records[0]
            count = driver.execute_query("MATCH (n) RETURN count(n) AS c").records[0]["c"]
    except Exception as e:  # 연결·인증 오류. 메시지에 비밀번호는 없다
        print("실패:", type(e).__name__, str(e)[:200])
        return 1
    print(f"성공: {name} {v} ({edition}), 노드 {count}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
