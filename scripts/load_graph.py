"""정규화된 Product와 기사(정답 라벨·자동 추출)를 Neo4j에 적재한다. 여러 번 실행해도 같은 그래프가 나온다.

실행:  python scripts/load_graph.py                 # 제약·인덱스 + 표준 값 + 02·03 Product + 라벨 + Term 재구성
       python scripts/load_graph.py --labels-only   # 기사(Style·Item·Article)와 Term만 다시 적재 (라벨을 고친 뒤)
       python scripts/load_graph.py --from-extractions "$SHARED/extractions/extract-v1"   # T1 자동 추출 기사도 (라벨 우선)
       python scripts/load_graph.py --prune-products # 입력 파일에 없는 Product 삭제 (products v2로 id가 바뀐 뒤)
       python scripts/load_graph.py --reset         # 그래프를 비우고 처음부터 (확인 없이 전부 삭제하므로 주의)
입력:  data/processed/otpensource_all.jsonl, musinsa_snap_all.jsonl  (build_otpensource.py, build_musinsa_snap.py)
       eval/datasets/article_extraction/labels/*.json              (팀 내부로 전달받는 파일, README 참고)
       data/raw/vogue_kr/articles_pool_150.jsonl                   (크롤 기사 — Item 이미지 맵 {image_idx: url})
       $SHARED/terms/term_map.jsonl                                (Term 계층 원본, 없으면 Style aliases만)
선행:  docker compose up -d  (Neo4j), .env의 NEO4J_* 설정. 테스트 인스턴스는 NEO4J_URI=bolt://localhost:7688
쓰기 권한: Neo4j 7687 쓰기·--reset은 T3·L0만 (팀 규약 §7).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neo4j import GraphDatabase  # noqa: E402

from trend_agent.config import neo4j_settings  # noqa: E402
from trend_agent.graph import loader  # noqa: E402
from trend_agent.graph.terms import rebuild_terms  # noqa: E402
from trend_agent.resolver import term_map  # noqa: E402

PROC = ROOT / "data" / "processed"
LABELS = ROOT / "eval" / "datasets" / "article_extraction" / "labels"
CRAWL = ROOT / "data" / "raw" / "vogue_kr" / "articles_pool_150.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels-only", action="store_true", help="Product는 건드리지 않고 기사·Term만")
    ap.add_argument("--reset", action="store_true", help="MATCH (n) DETACH DELETE n 후 다시 적재")
    ap.add_argument("--products-dir", default=str(PROC), help="정규화 Product JSONL 폴더 (기본 data/processed)")
    ap.add_argument("--labels-dir", default=str(LABELS))
    ap.add_argument("--from-extractions", metavar="DIR", help="T1 자동 추출 폴더 $SHARED/extractions/<prompt_version>")
    ap.add_argument("--prefer", choices=["labels", "extractions"], default="labels", help="같은 기사가 둘 다 있을 때")
    ap.add_argument("--crawl", default=str(CRAWL), help="크롤 JSONL (Item 이미지 맵·기사 메타)")
    ap.add_argument("--terms", default=None, help="term_map.jsonl (기본 $SHARED/terms/term_map.jsonl)")
    ap.add_argument("--prune-products", action="store_true", help="이번 입력에 없는 Product 삭제")
    args = ap.parse_args()

    s = neo4j_settings()
    t0 = time.time()
    report: dict = {"uri": s.uri}
    with GraphDatabase.driver(s.uri, auth=(s.user, s.password)) as driver:
        driver.verify_connectivity()
        if args.reset:
            driver.execute_query("MATCH (n) DETACH DELETE n")
            print("그래프를 비웠다")
        report.update(loader.ensure_schema(driver))
        print("표준 값 노드:", loader.seed_vocabulary(driver))
        if not args.labels_only:
            seen: set[str] = set()
            for name in ("otpensource_all.jsonl", "musinsa_snap_all.jsonl"):
                path = Path(args.products_dir) / name
                if not path.exists():
                    print(f"건너뜀: {name} 없음 (정규화 스크립트를 먼저 실행)")
                    continue
                print(name, loader.load_products(driver, loader.read_products(path), seen=seen))
            if args.prune_products:
                if seen:
                    report["pruned_products"] = loader.prune_products(driver, seen)
                else:
                    print("prune 건너뜀: 적재한 Product가 없다")
        labels = Path(args.labels_dir)
        if not (labels.exists() and any(labels.glob("*.json"))) and not args.from_extractions:
            print(f"라벨 없음: {labels} (팀 내부로 전달받아 이 위치에 둔다)")
        crawl = args.crawl if Path(args.crawl).exists() else None
        if crawl is None:
            print(f"크롤 JSONL 없음: {args.crawl} → Item 이미지 맵 없이 적재")
        arts = loader.load_articles(driver, label_dir=labels, extraction_dir=args.from_extractions,
                                    crawl_path=crawl, prefer=args.prefer)
        for aid, stats in arts.items():
            print("기사", aid, stats)
        report["articles"] = {"label": sum(1 for x in arts.values() if x["from"] == "label"),
                              "extraction": sum(1 for x in arts.values() if x["from"] == "extraction"),
                              "image_links_unmapped": sum(x.get("image_links_unmapped", 0) for x in arts.values())}
        terms_path = Path(args.terms) if args.terms else term_map.default_path()
        rows = term_map.read_term_map(terms_path)
        report["terms_source"] = str(terms_path) if terms_path.exists() else f"(없음) {terms_path}"
        report["terms"] = rebuild_terms(driver, rows)
        report["counts"] = loader.graph_counts(driver)
    report["seconds"] = round(time.time() - t0, 1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
