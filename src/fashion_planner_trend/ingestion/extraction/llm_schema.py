"""기사 추출용 LLM 스키마 — `ArticleExtraction`의 strict 변환 + 추출 전용 변형 2개.

1. `ImageLink` → `{image_block_idx, basis}`: LLM은 프롬프트의 블록 번호 하나만 쓴다. 코드가
   `blocks[image_block_idx].image_idx`(= 크롤러 `images[].image_idx`)로 바꿔 저장한다.
   크롤러 이미지 순번 ≠ block idx가 1,967장 중 1,940장이라 두 번호 체계를 LLM에 섞어 보이지 않는다.
2. `ExtractedDetail` → detail·neckline·collar 3변형 anyOf: `type`↔`value` enum 대응을 스키마가 강제한다.

LLM에 보이지 않는 필드: `article_id`(코드가 넣음), Item의 `size`(항상 null)·`review_flags`(코드가 채움).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable

from ...llm.schema import schema_sha256, strict_schema
from ...schemas.article_extraction import ArticleExtraction
from ...schemas.vocabulary import DETAIL_ENUMS

EXCLUDE = {"ArticleExtraction": {"article_id"}, "ExtractedItem": {"size", "review_flags"}}

__all__ = ["extraction_schema", "EXCLUDE"]


def _image_block_override(target: dict, conv: Callable) -> dict:
    basis = target["properties"]["basis"]["enum"]
    return {
        "type": "object",
        "properties": {"image_block_idx": {"type": "integer"}, "basis": {"type": "string", "enum": list(basis)}},
        "required": ["image_block_idx", "basis"],
        "additionalProperties": False,
    }


def _nullable_str() -> dict:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _detail_variants_override(target: dict, conv: Callable) -> dict:
    variants = []
    for dtype, enum in DETAIL_ENUMS.items():
        variants.append({
            "type": "object",
            "properties": {
                "raw": {"type": "string"},
                "type": {"type": "string", "enum": [dtype.value]},
                "value": {"anyOf": [{"type": "string", "enum": [m.value for m in enum]}, {"type": "null"}]},
                "motif": _nullable_str(),
                "placement": _nullable_str(),
            },
            "required": ["raw", "type", "value", "motif", "placement"],
            "additionalProperties": False,
        })
    return {"anyOf": variants}


@lru_cache(maxsize=1)
def _cached() -> tuple[dict, tuple[str, ...], str]:
    schema, dropped = strict_schema(
        ArticleExtraction, exclude=EXCLUDE,
        overrides={"ImageLink": _image_block_override, "ExtractedDetail": _detail_variants_override},
    )
    return schema, tuple(dropped), schema_sha256(schema)


def extraction_schema() -> tuple[dict, list[str], str]:
    """(LLM용 strict 스키마, 버린 제약, sha256). 버린 제약(`minItems=1` 2개)은 pydantic 재검증이 잡는다."""
    schema, dropped, sha = _cached()
    import copy
    return copy.deepcopy(schema), list(dropped), sha
