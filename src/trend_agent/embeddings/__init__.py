"""로컬 임베딩 단일 진입점 — 계약 v1.1 (L0 작성, G1). 구현: C1 (`runtime.py` · 배치 `batch.py` · 표준 텍스트 `text.py`).

사용자 결정 Q2: 과금 없는 로컬 오픈소스 모델(bge-m3)을 쓴다. Neo4j 노드에는 임베딩을 저장하지 않는다
(검색이 노드 전체를 가져오고 재적재 때 지워지기 때문). Neo4j 벡터 인덱스는 선택이다.

공개 이름과 시그니처 (구현은 `runtime.py`):

    def embed_texts(texts: list[str], model: str | None = None) -> numpy.ndarray
        # float32, shape (N, 1024), L2 정규화. 기본 모델 = 활성 manifest의 모델, 없으면 "BAAI/bge-m3".
        # 허용 모델: BAAI/bge-m3, (사용자 승인 대체) intfloat/multilingual-e5-large.
        # bge-m3는 prefix 없음. e5는 문서 "passage: ", 질의 "query: ".
        # 질의 호출 중에 조용히 다른 모델로 바꾸지 않는다(모델 전환은 배치에서만, 사유 기록).
    def search(label: str, query_vec, k: int = 10) -> list[tuple[str, float]]
        # label ∈ Product | Item | Style | Term (대소문자 무관). manifest가 가리키는 parquet (id, text_sha, vector)를
        # 메모리 numpy 코사인으로 검색한다. 점수 = 코사인(-1~1). 동점은 id 오름차순.
        # 빈 라벨은 [], 미생성 파일·차원 불일치·NaN·0벡터는 명시적 예외. Neo4j에 접근하지 않는다.

ID 규칙: Product = Product.id, Style·Item = graph/loader.py 규칙(`<aid>-s<n>`, `<aid>-s<n>-i<m>`),
Term = graph_schema.term_id(normalize_term(text)) ("term:<norm>"). 경로: env `VOGUE_EMBEDDINGS_DIR`
(기본은 구현 문서), 모델 캐시는 HF_HOME. **import만으로 모델 다운로드·torch 로딩을 시작하지 않는다**(지연 로딩).
이 파일은 계약이라 L0만 고친다(바꿀 때는 contract_change).
"""
from __future__ import annotations

import importlib
from typing import Any

__all__ = ["embed_texts", "search"]

_SOURCE = {name: ".runtime" for name in __all__}


def __getattr__(name: str) -> Any:
    source = _SOURCE.get(name)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(source, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
