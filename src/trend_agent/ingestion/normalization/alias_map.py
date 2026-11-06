"""별칭 사전 적용. 문서: docs/schemas/sources/alias_map.md, 사전 데이터: configs/alias_map/*.yaml

변환 순서: 표준과 일치 → 별칭 → 근사 별칭(approx) → 속성 간 이동(route) → 힌트(hint) → 값 없음 → 매핑 불가.
표준 밖 값은 억지로 가장 비슷한 값에 넣지 않는다 (근사 별칭으로 명시한 것만 예외).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from trend_agent.schemas import vocabulary as V

DEFAULT_DIR = Path(__file__).resolve().parents[4] / "configs" / "alias_map"

# 속성 이름 → 표준 Enum
STANDARD: dict[str, type[Enum]] = {
    "color": V.Color,
    "category": V.CategoryMinor,
    "material": V.Material,
    "pattern": V.Pattern,
    "detail": V.DetailName,
    "neckline": V.Neckline,
    "collar": V.Collar,
    "fit": V.Fit,
    "shape": V.Shape,
    "body_length": V.BodyLength,
    "sleeve_length": V.SleeveLength,
}

_COMPOUNDS = ("시퀸/", "비닐/", "울/")  # 쉼표·슬래시로 나누면 안 되는 표준 값 일부


def norm_key(text: Any) -> str:
    """소문자로 바꾸고 공백·-·_ 를 뺀다."""
    return re.sub(r"[\s\-_]", "", str(text)).lower()


@dataclass
class Normalized:
    raw: str
    value: Optional[str] = None
    approx: bool = False
    route_to: Optional[str] = None  # 다른 속성의 값이 들어온 경우
    hint: dict[str, Any] = field(default_factory=dict)  # 예: {"category_minor": "패딩"}
    matched_by: str = "none"  # standard | alias | approx | route | hint | null_value | none


class AliasMap:
    def __init__(self, config_dir: Path | str = DEFAULT_DIR):
        self.dir = Path(config_dir)
        self._cfg: dict[str, dict[str, Any]] = {}
        self._index: dict[str, dict[str, tuple[str, str]]] = {}  # attr -> key -> (value, kind)
        for path in sorted(self.dir.glob("*.yaml")):
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            self._cfg[cfg["attribute"]] = cfg
        for attr, enum in STANDARD.items():
            cfg = self._cfg.get(attr, {})
            idx: dict[str, tuple[str, str]] = {}
            for member in enum:
                idx[norm_key(member.value)] = (member.value, "standard")
            for value, raws in (cfg.get("aliases") or {}).items():
                for raw in raws:
                    idx.setdefault(norm_key(raw), (value, "alias"))
            for value, raws in (cfg.get("approx") or {}).items():
                for raw in raws:
                    idx.setdefault(norm_key(raw), (value, "approx"))
            self._index[attr] = idx

    # ---------- 단일 값 ----------
    def normalize(self, attribute: str, raw: Any) -> Normalized:
        text = "" if raw is None else str(raw).strip()
        out = Normalized(raw=text)
        if not text:
            return out
        key = norm_key(text)
        cfg = self._cfg.get(attribute, {})
        hit = self._index.get(attribute, {}).get(key)
        if hit:
            value, kind = hit
            out.value, out.matched_by, out.approx = value, kind, kind == "approx"
            return out
        route = {norm_key(k): v for k, v in (cfg.get("route") or {}).items()}.get(key)
        if route:
            out.route_to, out.value, out.matched_by = route["to"], route.get("value"), "route"
            return out
        hint = {norm_key(k): v for k, v in (cfg.get("hint") or {}).items()}.get(key)
        if hint:
            out.hint, out.matched_by = dict(hint), "hint"
            return out
        if key in {norm_key(x) for x in (cfg.get("null_values") or [])}:
            out.matched_by = "null_value"
        return out

    # ---------- 복수 값 ----------
    @staticmethod
    def split_tokens(raw: Any) -> list[str]:
        text = "" if raw is None else str(raw).strip()
        if not text:
            return []
        if any(c in text for c in _COMPOUNDS):
            return [text]
        return [t.strip() for t in re.split(r"[,/+&:]", text) if t.strip()]

    def normalize_tokens(self, attribute: str, raw: Any) -> list[Normalized]:
        if attribute == "color":
            return self.normalize_color(raw)
        out = []
        for tok in self.split_tokens(raw):
            n = self.normalize(attribute, tok)
            if n.matched_by == "none":  # 소재의 "울 블렌드" 같은 꼬리표 제거 후 한 번 더
                stripped = re.sub(r"블렌드|혼방", "", tok).strip()
                if stripped and stripped != tok:
                    n2 = self.normalize(attribute, stripped)
                    if n2.matched_by != "none":
                        n2.raw = tok
                        n = n2
            out.append(n)
        return out

    def normalize_color(self, raw: Any) -> list[Normalized]:
        """색: 복수 색 분리 → 수식어 제거 → 끝의 기본색(근사)."""
        cfg = self._cfg.get("color", {})
        modifiers = [norm_key(m) for m in cfg.get("modifiers", [])]
        base_keys = sorted(self._index["color"].keys(), key=len, reverse=True)
        results = []
        for tok in self.split_tokens(raw):
            key = norm_key(tok)
            if re.fullmatch(r"[\d.]+", key) or re.fullmatch(r"\d+color", key):
                results.append(Normalized(raw=tok, matched_by="null_value"))
                continue
            n = self.normalize("color", tok)
            if n.value is None and n.matched_by == "none":
                stripped = key
                for m in modifiers:
                    stripped = stripped.replace(m, "")
                if stripped and stripped != key and stripped in self._index["color"]:
                    value, _ = self._index["color"][stripped]
                    n = Normalized(raw=tok, value=value, matched_by="alias")
                elif key in ("멜란지", "melange"):
                    n = Normalized(raw=tok, value=V.Color.GREY.value, matched_by="alias")
                else:
                    # 끝의 기본색 ("차콜 그레이", "sage green"): 근사로 표시
                    for b in base_keys:
                        if len(b) >= 2 and stripped.endswith(b):
                            value, _ = self._index["color"][b]
                            n = Normalized(raw=tok, value=value, approx=True, matched_by="approx")
                            break
            results.append(n)
        return results

    # ---------- 02 카테고리 ----------
    def map_otpensource_category(
        self, sub_category: Any, product_name: Any = "", big_category: Any = ""
    ) -> "OtpenCategory":
        """02 소분류 → 표준. 소분류가 비어 있으면 대분류로 상분류만 정한다 (상의·아우터·바지→하의).

        `원피스_스커트`의 빈 소분류는 상품명에 스커트/원피스가 있을 때만 정한다.
        """
        cfg = self._cfg["category_otpensource"]
        sub = "" if sub_category is None else str(sub_category).strip()
        name = "" if product_name is None else str(product_name)
        row = cfg["subcategories"].get(sub)
        if row is None:
            big = "" if big_category is None else str(big_category).strip()
            fallback = cfg.get("big_category_fallback", {}).get(big)
            if not sub and fallback:
                return OtpenCategory(raw=sub, category_major=fallback)
            if not sub and big == "원피스_스커트":
                for word, (minor, major) in cfg.get("skirt_dress_name_fallback", {}).items():
                    if word in name:
                        return OtpenCategory(raw=sub, category_minor=minor, category_major=major)
            return OtpenCategory(raw=sub)
        minor, major, body, sleeve = row
        if minor is None and sub == "셔츠/블라우스":
            name = "" if product_name is None else str(product_name)
            for cand, words in cfg["blouse_shirt_keywords"].items():
                if any(w in name for w in words):
                    minor = cand
                    break
        return OtpenCategory(raw=sub, category_minor=minor, category_major=major,
                             length_body_raw=body, length_sleeve_raw=sleeve)


@dataclass
class OtpenCategory:
    raw: str
    category_minor: Optional[str] = None
    category_major: Optional[str] = None
    length_body_raw: Optional[str] = None
    length_sleeve_raw: Optional[str] = None
