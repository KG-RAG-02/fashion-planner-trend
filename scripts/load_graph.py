"""정규화된 Product와 정답 라벨을 Neo4j에 적재한다. 여러 번 실행해도 같은 그래프가 나온다.

실행:  python scripts/load_graph.py                 # 제약·인덱스 + 표준 값 + 02·03 Product + 라벨
       python scripts/load_graph.py --labels-only   # 라벨(Style·Item·Article)만 다시 적재 (라벨을 고친 뒤)
       python scripts/load_graph.py --reset         # 그래프를 비우고 처음부터 (확인 없이 전부 삭제하므로 주의)
입력:  data/processed/otpensource_all.jsonl, musinsa_snap_all.jsonl  (build_otpensource.py, build_musinsa_snap.py)
       eval/datasets/article_extraction/labels/*.json              (팀 내부로 전달받는 파일, README 참고)
선행:  docker compose up -d  (Neo4j), .env의 NEO4J_* 설정
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neo4j import GraphDatabase  # noqa: E402

from trend_agent.config import neo4j_settings  # noqa: E402
from trend_agent.graph import loader  # noqa: E402

PROC = ROOT / "data" / "processed"
LABELS = ROOT / "eval" / "datasets" / "article_extraction" / "labels"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels-only", action="store_true")
    ap.add_argument("--reset", action="store_true", help="MATCH (n) DETACH DELETE n 후 다시 적재")
    args = ap.parse_args()

    s = neo4j_settings()
    with GraphDatabase.driver(s.uri, auth=(s.user, s.password)) as driver:
        driver.verify_connectivity()
        if args.reset:
            driver.execute_query("MATCH (n) DETACH DELETE n")
            print("그래프를 비웠다")
        loader.ensure_schema(driver)
        print("표준 값 노드:", loader.seed_vocabulary(driver))
        if not args.labels_only:
            for name in ("otpensource_all.jsonl", "musinsa_snap_all.jsonl"):
                path = PROC / name
                if not path.exists():
                    print(f"건너뜀: {name} 없음 (정규화 스크립트를 먼저 실행)")
                    continue
                print(name, loader.load_products(driver, loader.read_products(path)))
        if LABELS.exists() and any(LABELS.glob("*.json")):
            for aid, stats in loader.load_labels(driver, LABELS).items():
                print("라벨", aid, stats)
        else:
            print(f"라벨 없음: {LABELS} (팀 내부로 전달받아 이 위치에 둔다)")
        print(json.dumps(loader.graph_counts(driver), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
