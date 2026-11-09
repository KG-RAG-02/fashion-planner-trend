"""pydantic 모델 → LLM용 strict JSON Schema 변환 (계약 v1.1 `strict_schema`·`schema_sha256`).

strict 규칙(OpenAI structured outputs):
- 모든 객체: `properties`의 키 전부가 `required`, `additionalProperties: false`
- Optional 필드는 `anyOf [..., {"type": "null"}]`로 남는다(pydantic 기본 출력) → "필수 + nullable"
- `$ref`/`$defs`는 펼친다(인라인). `default`·`title` 등 설명용 키는 버린다
- strict 모드가 받지 않는 제약 키(`minItems` 등)는 버리고 **목록으로 돌려준다** → 호출자가 pydantic 재검증으로 보완

응답은 반드시 원래 pydantic 모델로 다시 검증한다(`client.structured_model`).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Optional

__all__ = ["strict_schema", "schema_sha256", "check_instance", "KEEP_KEYS", "CONSTRAINT_KEYS"]

KEEP_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum", "anyOf", "description", "const"}
CONSTRAINT_KEYS = {
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "format", "uniqueItems",
}

Override = Callable[[dict, Callable[[dict], dict]], dict]


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def strict_schema(
    model_cls,
    *,
    exclude: Optional[dict[str, set[str]]] = None,
    overrides: Optional[dict[str, Override]] = None,
    keep_constraints: bool = False,
) -> tuple[dict, list[str]]:
    """pydantic 모델 클래스를 strict JSON Schema로 바꾼다.

    - `exclude`: {모델 이름: {필드, …}} — LLM에 보여 주지 않을 필드(예: 코드가 채우는 `review_flags`).
      제외한 필드는 pydantic 기본값으로 채워져야 한다(호출자 책임).
    - `overrides`: {모델 이름: fn(원래 정의, conv) -> 새 스키마} — 그 모델 자리를 통째로 바꾼다.
    - 반환: (스키마, 버린 제약 목록 `"경로.키=값"`).
    """
    raw = model_cls.model_json_schema()
    defs: dict[str, dict] = raw.get("$defs", {})
    exclude = exclude or {}
    overrides = overrides or {}
    dropped: list[str] = []

    def conv(node: dict, path: str, model_name: Optional[str] = None) -> dict:
        if "$ref" in node:
            name = _ref_name(node["$ref"])
            target = defs[name]
            if name in overrides:
                return overrides[name](target, lambda n, p=f"{path}<{name}>": conv(n, p))
            return conv(target, f"{path}<{name}>", name)
        out: dict[str, Any] = {}
        for key, val in node.items():
            if key in CONSTRAINT_KEYS:
                if keep_constraints:
                    out[key] = copy.deepcopy(val)
                else:
                    dropped.append(f"{path}.{key}={val}")
                continue
            if key not in KEEP_KEYS or key == "required":
                continue
            if key == "properties":
                skip = exclude.get(model_name or "", set())
                out["properties"] = {pk: conv(pv, f"{path}.{pk}") for pk, pv in val.items() if pk not in skip}
            elif key == "items":
                out["items"] = conv(val, f"{path}[]")
            elif key == "anyOf":
                out["anyOf"] = [conv(x, f"{path}|") for x in val]
            else:
                out[key] = copy.deepcopy(val)
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            out.setdefault("properties", {})
            out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False
        # pydantic의 `None` 전용 필드(`size: None`)는 {"type": "null"}로 남는다 — 그대로 둔다
        return out

    top = model_cls.__name__
    body = {k: v for k, v in raw.items() if k != "$defs"}
    return conv(body, top, top), dropped


def schema_sha256(schema: dict) -> str:
    """키 정렬·공백 없는 JSON의 sha256 (캐시 키·로그용)."""
    blob = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _type_ok(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _JSON_TYPES.get(t)
    return py is not None and isinstance(value, py) and not (t != "boolean" and isinstance(value, bool))


def check_instance(value: Any, schema: dict, path: str = "$", limit: int = 20) -> list[str]:
    """strict 스키마 부분집합(type·properties·required·additionalProperties·items·enum·const·anyOf)으로
    응답을 검사한다. 새 의존성 없이 `LLMOutputError`(스키마 불일치)를 판정하려는 용도 — 최종 검증은 pydantic."""
    errors: list[str] = []

    def walk(v: Any, s: dict, p: str) -> None:
        if len(errors) >= limit:
            return
        if "anyOf" in s:
            if not any(not _check(v, alt, p) for alt in s["anyOf"]):
                errors.append(f"{p}: anyOf 어느 것에도 맞지 않음")
            return
        t = s.get("type")
        if t is not None:
            types = t if isinstance(t, list) else [t]
            if not any(_type_ok(v, x) for x in types):
                errors.append(f"{p}: 타입 {types} 아님 ({type(v).__name__})")
                return
        if "const" in s and v != s["const"]:
            errors.append(f"{p}: const {s['const']!r} 아님")
        if "enum" in s and v not in s["enum"]:
            errors.append(f"{p}: enum 밖 값 {v!r}")
        if isinstance(v, dict) and "properties" in s:
            props = s["properties"]
            for k in s.get("required", []):
                if k not in v:
                    errors.append(f"{p}.{k}: 필수 필드 없음")
            if s.get("additionalProperties") is False:
                for k in v:
                    if k not in props:
                        errors.append(f"{p}.{k}: 스키마에 없는 필드")
            for k, sub in props.items():
                if k in v:
                    walk(v[k], sub, f"{p}.{k}")
        if isinstance(v, list) and "items" in s:
            for i, x in enumerate(v):
                walk(x, s["items"], f"{p}[{i}]")

    def _check(v: Any, s: dict, p: str) -> list[str]:
        saved = len(errors)
        walk(v, s, p)
        new = errors[saved:]
        del errors[saved:]
        return new

    walk(value, schema, path)
    return errors
