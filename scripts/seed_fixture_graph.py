"""합성 stub 그래프를 NEO4J_URI·NEO4J_USER·NEO4J_PASSWORD의 Neo4j에 적재한다 (stub 프로필 `neo4j-stub` 용).

실행:  NEO4J_URI=bolt://127.0.0.1:7688 NEO4J_USER=neo4j NEO4J_PASSWORD=<로컬값> python scripts/seed_fixture_graph.py
       ... python scripts/seed_fixture_graph.py --allow-wipe   # 비어 있지 않은 그래프를 비우고 적재 (일회용 인스턴스만)
       컨테이너: docker run --rm --read-only --tmpfs /tmp -e NEO4J_URI=bolt://neo4j-stub:7687 ... vogue/trend:dev \
                 python scripts/seed_fixture_graph.py
데이터: src/fashion_planner_trend/graph/fixture.py — Neo4j 테스트 그래프 + 기사 900003 "발레코어"(합성, Vogue 원문 없음).
확인:  적재 뒤 stub 질의(examples/stub/trend-request.json의 input)를 실행해 요약을 출력한다.
       status가 ok이고 trends·evidence가 비어 있지 않으면 exit 0, 아니면 exit 1.
안전:  - 접속값은 프로세스 환경변수에서만 읽는다(`.env`를 읽지 않는다). NEO4J_URI가 없으면 거부한다 (exit 2).
       - Neo4j Aura 같은 관리형 주소(*.neo4j.io)는 항상 거부한다 (exit 2).
       - 그래프가 비어 있지 않으면 --allow-wipe 없이는 거부한다 (exit 2).
       - 비밀번호는 출력하지 않는다. 연결 실패는 exit 1.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fashion_planner_trend.config import neo4j_driver, neo4j_settings  # noqa: E402
from fashion_planner_trend.graph import fixture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--allow-wipe", action="store_true", help="비어 있지 않은 그래프를 모두 지우고 적재 (stub 전용 인스턴스만)")
    args = ap.parse_args(argv)
    # 작은 합성 그래프에는 HAS_PATTERN 등이 없어 서버 안내(warning)가 질의마다 찍힌다. 출력은 요약만 남긴다
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

    env = {k: v for k, v in os.environ.items() if k.startswith("NEO4J_")}  # `.env`는 읽지 않는다
    if not env.get("NEO4J_URI", "").strip():
        print("거부: NEO4J_URI 환경변수가 없습니다 (이 스크립트는 .env를 읽지 않습니다).", file=sys.stderr)
        return 2
    settings = neo4j_settings(env)
    if fixture.is_managed_cloud(settings.uri):
        print("거부: 관리형 Neo4j(Aura, *.neo4j.io)에는 stub 그래프를 적재하지 않습니다.", file=sys.stderr)
        return 2
    parts = urlsplit(settings.uri)
    target = f"{parts.scheme}://{parts.hostname}:{parts.port or 7687}"
    try:
        with neo4j_driver(settings) as driver:
            driver.verify_connectivity()
            try:
                report = fixture.seed_stub_graph(driver, allow_wipe=args.allow_wipe)
            except fixture.GraphNotEmpty as exc:
                print(f"거부: {target}의 그래프에 노드 {exc.count}개가 있습니다. "
                      "stub 전용 일회용 인스턴스라면 --allow-wipe로 비우고 적재하세요.", file=sys.stderr)
                return 2
            result = fixture.run_stub_query(driver)
    except Exception as exc:  # 연결·인증 오류. 메시지에 비밀번호는 없다
        print(f"실패: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1
    summary = fixture.summarize_result(result)
    print(json.dumps({
        "target": target, "wiped_nodes": report["wiped_nodes"], "counts": report["counts"],
        "stub_query": {"request_id": fixture.STUB_REQUEST_ID, "status": summary["status"],
                       "trends": [t["style"]["name"] for t in summary["trends"]],
                       "evidence": len(summary["evidence_ids"])},
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" and result["trends"] and result["evidence"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
