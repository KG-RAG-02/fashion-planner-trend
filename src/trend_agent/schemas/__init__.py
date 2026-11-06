"""Internal domain schemas used by the trend analysis agent."""

from .trend_result import (
    AgentResult,
    AttributeSignal,
    TrendCandidate,
    TrendResult,
    trend_result_from_mapping,
    trend_result_to_mapping,
)

__all__ = [
    "AgentResult",
    "AttributeSignal",
    "TrendCandidate",
    "TrendResult",
    "trend_result_from_mapping",
    "trend_result_to_mapping",
]
