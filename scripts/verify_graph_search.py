"""그래프 검색 결과가 "모든 Product를 직접 점수 계산한 결과"와 같은지 확인한다 (후보 탐색이 점수 있는 상품을 놓치지 않는지).

각 Style의 Item마다:
  A) 그래프 검색(`TrendSearcher.candidate_ids` → 점수) 상위 k
  B) 정규화 파일의 같은 상분류 Product 전부를 `rank_products`로 점수 계산한 상위 k
두 목록(id, 점수)이 같아야 한다. 다르면 후보 탐색 Cypher에 빠지는 경우가 있다는 뜻이다.

실행:  python scripts/verify_graph_search.py [--top-k 5]
선행:  python scripts/load_graph.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neo4j import GraphDatabase  # noqa: E402

from trend_agent.config import neo4j_settings  # noqa: E402
from trend_agent.graph.loader import read_products  # noqa: E402
from trend_agent.graph.search import ITEMS_CYPHER, TrendSearcher, _record  # noqa: E402
from trend_agent.retrieval.scoring import attribute_values, rank_products  # noqa: E402
from trend_agent.schemas.graph_schema import SCORED_ATTRIBUTES, ItemNode, ItemRecord  # noqa: E402

PROC = ROOT / "data" / "processed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    k = ap.parse_args().top_k

    products = [r for name in ("otpensource_all.jsonl", "musinsa_snap_all.jsonl")
                for r in read_products(PROC / name) if r.in_graph_scope]
    by_major: dict[str, list] = {}
    for p in products:
        by_major.setdefault(p.category_major.value, []).append(p)
    print(f"정규화 파일의 Product {len(products)}개")

    s = neo4j_settings()
    bad = total = 0
    with GraphDatabase.driver(s.uri, auth=(s.user, s.password)) as driver:
        searcher = TrendSearcher(driver, top_k=k)
        styles = [dict(r["s"]) for r in driver.execute_query("MATCH (s:Style) RETURN s ORDER BY s.id").records]
        for st in styles:
            rows = driver.execute_query(ITEMS_CYPHER, style_ids=[st["id"]]).records
            for r in rows:
                item: ItemRecord = _record(ItemRecord, ItemNode, r)
                core, sup = st.get("focus_core", []), st.get("focus_supporting", [])
                has_attrs = any(attribute_values(item, a) for a in SCORED_ATTRIBUTES)
                ids = searcher.candidate_ids(item, limit_when_no_attributes=None if has_attrs else k)
                graph = rank_products(item, searcher._fetch_products(ids), searcher.weights, core, sup, top_k=k)
                brute = rank_products(item, by_major[item.category_major.value], searcher.weights, core, sup, top_k=k)
                g = [(x.product.node.id, round(x.score, 6)) for x in graph]
                b = [(x.product.node.id, round(x.score, 6)) for x in brute]
                total += 1
                ok = g == b
                bad += not ok
                print(f"{'OK  ' if ok else 'DIFF'} {st['name']} / {item.node.name}  (후보 {len(ids)}개, 상위 {len(g)}개)")
                if not ok:
                    print("   그래프:", g)
                    print("   전수  :", b)
    print(f"\n{total}개 Item 중 불일치 {bad}개")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
