"""Trend Resolver (T3) — 용어를 그래프 트렌드(Tier1)·어휘 규칙(Tier2)·의미 유사도(Tier3)·LLM 등급(Tier4) 순서로 해석한다.

진입점: `TrendResolver(searcher, ResolveConfig(...)).run(terms, request_id)` → `TrendAnalysisResult` v1.1.
정규화는 계약 함수 `trend_agent.schemas.graph_schema.normalize_term()` 하나다.
"""
from .resolver import ResolveConfig, Tier3Candidate, TrendResolver

__all__ = ["ResolveConfig", "Tier3Candidate", "TrendResolver"]
