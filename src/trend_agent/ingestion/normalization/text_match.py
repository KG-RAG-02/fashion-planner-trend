"""02 상품명·`feature` 텍스트에서 표준 값 키워드를 찾는다 (alias_map.md §10, 버그 (f) 수정).

v1은 상품명과 feature를 이어 붙인 뒤 공백을 지우고 부분 문자열로 찾아서 공백을 넘는 오탐이 났다
(`빈티지 브라운`→지브라, `PUFFER`→puff, `비즈니스`→비즈). 규칙:
- 텍스트를 토큰으로 나눈다: 공백·괄호·`/ , + & | · : ; _` 등. 하이픈은 토큰 안에 둔다(`A-LINE`).
- 한국어 키(한글이 들어간 키): 토큰 안 부분 일치를 허용한다(`반집업`·`체크셔츠`·`빅로고`는 정탐).
  단 가드 단어(`configs/alias_map/text_guard.yaml`, 예: 비즈→비즈니스)가 토큰에 있으면 버린다.
- 영문 키: 토큰이 키(복수형 키는 단수 어간도)와 같거나 + 굴절 접미사(s, es, d, ed, ded, ered, y, ie, ies, ing)일 때만
  맞다(`pockets` 키 → `POCKET`·`POCKETS`, `stud` → `STUDDED`, `STUDIO`·`PUFFER`는 아님).
- 이웃한 두 토큰은 이어 붙인 값이 키와 **완전히 같을 때만** 맞다(`BOOTS CUT`=bootscut, `드롭 숄더`=드롭숄더).
  한국어 키는 두 번째 토큰 **전체**를 덮을 때도 맞다(`라쿤퍼 트리밍`=…퍼+트리밍). 두 번째 토큰의 앞부분만
  걸치는 경우(`빈티지 브라운`→지+브라, `트래블러 플리스`→러+플)는 오탐이라 버린다.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .alias_map import norm_key

_SPLIT = re.compile(r"[\s\[\]\(\)\{\}<>/,+&|·•:;_\"!?~*#@=]+")
_HANGUL = re.compile(r"[가-힣]")
EN_SUFFIXES = ("", "s", "es", "d", "ed", "ded", "ered", "y", "ie", "ies", "ing")


def tokenize(text: Optional[str]) -> list[str]:
    """원문 대소문자를 유지한 토큰 목록."""
    if not text:
        return []
    return [t for t in _SPLIT.split(str(text)) if t and t.strip("-")]


def is_hangul_key(key: str) -> bool:
    return bool(_HANGUL.search(key))


def english_stems(key: str) -> tuple[str, ...]:
    """영문 키의 어간: 키 자체 + 복수형이면 단수(pockets→pocket, pleats→pleat)."""
    stems = [key]
    if key.endswith("es") and len(key) > 4:
        stems.append(key[:-2])
    if key.endswith("s") and len(key) > 3:
        stems.append(key[:-1])
    return tuple(stems)


def english_match(token_norm: str, key: str, suffixes: Iterable[str] = EN_SUFFIXES) -> bool:
    return any(token_norm.startswith(stem) and token_norm[len(stem):] in suffixes for stem in english_stems(key))


class TextMatcher:
    """keys: [(정규화 키, 표준 값, approx)] — 정확 키를 근사 키보다 먼저, 같은 종류 안에서는 긴 키부터 검사한다.
    guards: {키 원문: [가드 단어]}."""

    def __init__(self, keys: list[tuple[str, str, bool]], guards: Optional[dict[str, list[str]]] = None):
        self.keys = sorted(keys, key=lambda x: (x[2], -len(x[0]), x[0]))
        self.guards = {norm_key(k): [norm_key(g) for g in v] for k, v in (guards or {}).items()}

    def _guarded(self, key: str, token_norm: str) -> bool:
        return any(g in token_norm for g in self.guards.get(key, ()))

    def match_key(self, key: str, toks: list[str], norms: list[str]) -> Optional[str]:
        """키가 맞은 원문 토큰(이웃 두 토큰이면 공백으로 이은 값). 없으면 None."""
        hangul = is_hangul_key(key)
        for tok, n in zip(toks, norms):
            if hangul:
                if key in n and not self._guarded(key, n):
                    return tok
            elif english_match(n, key):
                return tok
        for i in range(len(toks) - 1):
            pair = norms[i] + norms[i + 1]
            if pair == key:
                return f"{toks[i]} {toks[i + 1]}"
            # 한국어 키가 앞 토큰의 끝 + 뒤 토큰 전체와 같을 때 (라쿤퍼 트리밍 → 퍼트리밍)
            if hangul and norms[i + 1] and key.endswith(norms[i + 1]) and len(key) > len(norms[i + 1]) \
                    and norms[i].endswith(key[: len(key) - len(norms[i + 1])]) and not self._guarded(key, pair):
                return f"{toks[i]} {toks[i + 1]}"
        return None

    def find(self, text: Optional[str]) -> list[tuple[str, str, bool]]:
        """[(표준 값, 원문 토큰, approx)] — 키 검사 순서대로. 같은 값이 여러 키로 맞으면 처음 것만."""
        toks = tokenize(text)
        norms = [norm_key(t) for t in toks]
        out: list[tuple[str, str, bool]] = []
        seen: set[str] = set()
        for key, value, approx in self.keys:
            if value in seen:
                continue
            raw = self.match_key(key, toks, norms)
            if raw is not None:
                out.append((value, raw, approx))
                seen.add(value)
        return out
