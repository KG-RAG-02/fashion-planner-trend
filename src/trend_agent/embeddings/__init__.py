"""로컬 임베딩 단일 진입점. 구현: `runtime.py`.

사용자 결정: 과금 없는 로컬 오픈소스 모델(BAAI/bge-m3)을 쓴다. Neo4j 노드에는 임베딩을 저장하지 않는다.

    def embed_texts(texts: list[str], model: str | None = None) -> numpy.ndarray
        # float32, shape (N, 1024), L2 정규화. 기본 모델 "BAAI/bge-m3" (prefix 없음).
        # 허용 모델: BAAI/bge-m3, (사용자 승인 대체) intfloat/multilingual-e5-large.

**import만으로 모델 다운로드·torch 로딩을 시작하지 않는다**(지연 로딩). 모델 캐시는 `HF_HOME`(기본 Hugging Face 캐시)이다.
Style 해소(이름+정의 임베딩)가 이 함수를 쓴다.
"""
from __future__ import annotations

import importlib
from typing import Any

__all__ = ["embed_texts"]

_SOURCE = {name: ".runtime" for name in __all__}


def __getattr__(name: str) -> Any:
    source = _SOURCE.get(name)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(source, __name__), name)
    globals()[name] = value
    return value
