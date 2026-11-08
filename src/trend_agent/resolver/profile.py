"""가상 프로필 — 그래프에 없는 용어(Tier2 어휘 규칙·Tier4 LLM 등급)로 만든 트렌드와 Item.

가상 Item은 Neo4j에 쓰지 않는다. `ItemRecord`만 만들어 속성 노드 출발 후보 탐색(`TrendSearcher.candidate_ids`)과
같은 점수 함수로 상품을 찾는다. 수치(값 가중치·Item 비중)는 코드가 등급으로 만든다(weights.yaml `tier4`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from trend_agent.retrieval.weights import WeightsConfig
from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import AttrEdge, DetailEdge, ItemNode, ItemRecord
from trend_agent.schemas.trend_analysis_result import virtual_item_id, virtual_style_id

ValueRef = tuple[str, str]  # (값, value_level primary|secondary)


@dataclass
class VirtualItemSpec:
    category_major: str
    category_minor: Optional[str] = None
    level: str = "core"                                      # core | supporting → item_focus
    colors: list[ValueRef] = field(default_factory=list)
    materials: list[ValueRef] = field(default_factory=list)
    patterns: list[ValueRef] = field(default_factory=list)
    details: list[ValueRef] = field(default_factory=list)    # 값 = "<type>:<name>"
    fit: Optional[str] = None
    fit_level: str = "primary"

    def label(self, term: str) -> str:
        cat = self.category_minor or self.category_major
        return f"{term} · {cat}"

    def value_weights(self, weights: WeightsConfig) -> dict[str, dict[str, float]]:
        """{속성: {값: 가중치}} — 등급 → tier4.value_weights. 모두 primary면 전부 1.0(= 값 가중치 없음과 같은 점수)."""
        vw = weights.tier4.value_weights
        out: dict[str, dict[str, float]] = {}
        for attr, refs in (("color", self.colors), ("material", self.materials), ("pattern", self.patterns),
                           ("detail", self.details)):
            if refs:
                out[attr] = {v: vw[lvl] for v, lvl in refs}
        if self.fit:
            out["fit"] = {self.fit: vw[self.fit_level]}
        return out

    def record(self, item_id: str, name: str) -> ItemRecord:
        details = []
        for v, _ in self.details:
            t, n = v.split(":", 1)
            details.append(DetailEdge(type=V.DetailType(t), value=n))
        return ItemRecord(
            node=ItemNode(id=item_id, name=name, raw_text="", article_id="virtual",
                          category_minor=self.category_minor, fit=self.fit),
            category_major=self.category_major,
            colors=[AttrEdge(value=v) for v, _ in self.colors], materials=[AttrEdge(value=v) for v, _ in self.materials],
            patterns=[AttrEdge(value=v) for v, _ in self.patterns], details=details)


@dataclass
class VirtualProfile:
    tier: int                      # 2 | 4
    term: str
    norm: str
    definition: str
    focus_core: list[str] = field(default_factory=list)
    focus_supporting: list[str] = field(default_factory=list)
    items: list[VirtualItemSpec] = field(default_factory=list)
    evidence: list[tuple[str, int, str]] = field(default_factory=list)  # (article_id, block_idx, quote) — Tier4 인용
    value_weighted: bool = False   # Tier4만 True (Tier2는 모두 primary라 v1.0 식)

    @property
    def style_id(self) -> str:
        return virtual_style_id(self.tier, self.norm)

    def item_id(self, k: int) -> str:
        return virtual_item_id(self.tier, self.norm, k)

    def item_focus(self, spec: VirtualItemSpec, weights: WeightsConfig) -> float:
        return weights.tier4.item_focus[spec.level] if self.tier == 4 else 1.0
