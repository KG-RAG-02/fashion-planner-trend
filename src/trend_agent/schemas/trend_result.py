"""Internal, evidence-preserving TrendResult domain models.

These models describe the trend agent's normalized result. They are not a
shared transport envelope; API serialization belongs in the API adapter once
the cross-agent contract is agreed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Mapping, TypeAlias


InputStatus: TypeAlias = Literal["ok", "partial", "no_results", "error"]
AttributeOrigin: TypeAlias = Literal[
    "user_requested", "evidence_observation", "agent_proposal", "unknown"
]
AttributeValue: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True)
class AgentResult:
    """Status and provenance metadata for one agent result."""

    agent_id: str
    request_id: str
    schema_version: str
    status: InputStatus
    evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttributeSignal:
    """One attribute value with its own provenance links."""

    name: str
    value: AttributeValue
    origin: AttributeOrigin
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrendCandidate:
    """A named trend candidate and only the attributes supported for it."""

    name: str
    qualification: str
    season: str | None = None
    category: str | None = None
    attributes: tuple[AttributeSignal, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrendResult:
    """Normalized output from trend analysis, before API serialization."""

    result: AgentResult
    candidates: tuple[TrendCandidate, ...] = ()


def trend_result_from_mapping(payload: Mapping[str, object]) -> TrendResult:
    """Build the internal model from an already-decoded mapping.

    This intentionally maps the agent's internal result shape only. It does
    not prescribe or infer a cross-agent wire envelope.
    """

    result_data = _mapping(payload.get("result"), "result")
    result = AgentResult(
        agent_id=_required_str(result_data, "agent_id", "result"),
        request_id=_required_str(result_data, "request_id", "result"),
        schema_version=_required_str(result_data, "schema_version", "result"),
        status=_status(result_data.get("status")),
        evidence_ids=_strings(result_data.get("evidence_ids", ()), "result.evidence_ids"),
        limitations=_strings(result_data.get("limitations", ()), "result.limitations"),
    )

    raw_candidates = payload.get("candidates", ())
    if not isinstance(raw_candidates, (list, tuple)):
        raise ValueError("candidates must be a list or tuple")
    candidates = tuple(_candidate(item, index) for index, item in enumerate(raw_candidates))
    return TrendResult(result=result, candidates=candidates)


def trend_result_to_mapping(trend_result: TrendResult) -> dict[str, object]:
    """Serialize the internal model as plain Python data without losing lineage."""

    return asdict(trend_result)


def _candidate(value: object, index: int) -> TrendCandidate:
    where = f"candidates[{index}]"
    data = _mapping(value, where)
    raw_attributes = data.get("attributes", ())
    if not isinstance(raw_attributes, (list, tuple)):
        raise ValueError(f"{where}.attributes must be a list or tuple")
    attributes = tuple(_attribute(item, index, attr_index) for attr_index, item in enumerate(raw_attributes))
    season = data.get("season")
    category = data.get("category")
    if season is not None and not isinstance(season, str):
        raise ValueError(f"{where}.season must be a string or null")
    if category is not None and not isinstance(category, str):
        raise ValueError(f"{where}.category must be a string or null")
    return TrendCandidate(
        name=_required_str(data, "name", where),
        qualification=_required_str(data, "qualification", where),
        season=season,
        category=category,
        attributes=attributes,
        evidence_ids=_strings(data.get("evidence_ids", ()), f"{where}.evidence_ids"),
    )


def _attribute(value: object, candidate_index: int, attribute_index: int) -> AttributeSignal:
    where = f"candidates[{candidate_index}].attributes[{attribute_index}]"
    data = _mapping(value, where)
    origin = data.get("origin", "unknown")
    if origin not in ("user_requested", "evidence_observation", "agent_proposal", "unknown"):
        raise ValueError(f"{where}.origin is not a supported attribute origin")
    attribute_value = data.get("value")
    if not isinstance(attribute_value, (str, int, float, bool)) and attribute_value is not None:
        raise ValueError(f"{where}.value must be a scalar or null")
    return AttributeSignal(
        name=_required_str(data, "name", where),
        value=attribute_value,
        origin=origin,
        evidence_ids=_strings(data.get("evidence_ids", ()), f"{where}.evidence_ids"),
    )


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    return value


def _required_str(data: Mapping[str, object], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}.{key} must be a non-empty string")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{where} must be a list of strings")
    return tuple(value)


def _status(value: object) -> InputStatus:
    if value not in ("ok", "partial", "no_results", "error"):
        raise ValueError("result.status must be ok, partial, no_results, or error")
    return value
