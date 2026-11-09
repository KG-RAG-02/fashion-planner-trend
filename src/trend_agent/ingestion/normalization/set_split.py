"""02 다중 의류 분해 — 셋업·세트·투피스·상하의처럼 **종류가 여럿인** 상품을 상품명 근거로 구성품 Product로 나눈다.

사용자 요구(T2 작업 1): 한 레코드에 여러 종류 의류가 있으면 분리한다. 크롭 금지(Q4) — 구성품은 부모 이미지 1장을 공유한다.
규칙 근거: 상품명의 세트 표지 규칙 · 사전: configs/alias_map/set_components.yaml · 계약 v1.1 ProductNode
(`parent_id`·`split_from="name_rule"`·`multipack`·`split_candidate`, 구성품 id `<원 id>#<k>`).

판정(decision)
- `split`: 서로 다른 의류 종류 ≥2이고, `+`(또는 세트 표지가 있을 때 `&`·`,`)로 나뉘어 있거나 세트 표지가 있다.
  단 구분자 없이 세트 표지가 구성품 **사이**에 있으면 라인명 + 단품이다 → `candidate`.
- `multipack`: N팩(2PACK·2팩·1+1)·`N종 세트`·같은 종류 반복(`슬랙스+슬랙스`)이고 종류 ≤1 → 나누지 않고 `multipack=true`.
- `candidate`: 세트 표지는 있으나 종류 ≤1, 택1(`선택`·`OR`), 표지가 구성품 사이 → 나누지 않고 `split_candidate=true`.
- `accessory_set`: 의류 1종 + 액세서리(넥타이·머플러·벨트 …) → 나누지 않는다(리뷰 CSV에만).
- `none`: 그 밖(세트 표지·`+`·팩 표지가 없으면 검사하지 않는다).

검수 덮어쓰기(`configs/alias_map/set_overrides.yaml`, 사람이 채택한 것만): 규칙 판정 뒤 product_id로 적용한다.
`split`은 적힌 구성품으로 나누고 `split_from="review"`(계약 v1.3), `clear_candidate`는 candidate 판정을 none으로 바꾼다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from trend_agent.schemas import vocabulary as V
from trend_agent.schemas.graph_schema import ProductNode, ProductRecord

from .alias_map import DEFAULT_DIR, norm_key
from .text_match import tokenize

_HANGUL = re.compile(r"[가-힣]")
_PACK = re.compile(r"(?<![0-9a-z])(\d{1,2})\s*-?\s*(?:pack|팩)(?![a-z])|1\s*\+\s*1", re.IGNORECASE)
_NSET = re.compile(r"(?<!\d)(\d{1,2})\s*종\s*(?:세트|set)", re.IGNORECASE)
_EN_SUFFIX = ("", "s", "es")


@dataclass
class Component:
    category_minor: Optional[str]
    category_major: str
    raw: str
    segment: int
    tok_start: int   # 구간 안 토큰 위치 (원문 텍스트 범위 계산용)
    tok_end: int
    text: str = ""   # 이 구성품의 속성을 찾을 원문 범위


@dataclass
class Decision:
    decision: str                      # split | multipack | candidate | accessory_set | none
    reason: str = ""
    components: list[Component] = field(default_factory=list)
    accessories: list[str] = field(default_factory=list)
    pack_size: Optional[int] = None
    markers: list[str] = field(default_factory=list)

    @property
    def kinds(self) -> list[str]:
        return list(dict.fromkeys(c.category_minor or c.category_major for c in self.components))


@dataclass
class _Hit:
    kind: str                 # garment | major | accessory
    key: str
    minor: Optional[str]
    major: Optional[str]
    raw: str
    tok_start: int
    tok_end: int


class SetSplitter:
    def __init__(self, config_path: Path | str = DEFAULT_DIR / "set_components.yaml"):
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self.keys: dict[str, tuple[str, Optional[str], Optional[str]]] = {}   # key -> (kind, minor, major)
        for minor, words in cfg["garments"].items():
            major = V.MINOR_TO_MAJOR[V.CategoryMinor(minor)].value
            for w in words:
                self.keys.setdefault(norm_key(w), ("garment", minor, major))
        for major, words in cfg["major_only"].items():
            for w in words:
                self.keys.setdefault(norm_key(w), ("major", None, V.CategoryMajor(major).value))
        for w in cfg["accessories"]:
            self.keys.setdefault(norm_key(w), ("accessory", None, None))
        self.modifiers = {norm_key(w) for w in cfg["modifiers"]}
        self.generic = set(cfg["generic_minors"])
        self.markers = [norm_key(w) for w in cfg["markers"]]
        self.ul_markers = [norm_key(w) for w in cfg["upper_lower_markers"]]
        self.choice_markers = [norm_key(w) for w in cfg["choice_markers"]]
        self.choice_tokens = {norm_key(w) for w in cfg["choice_tokens"]}
        self.guards = {norm_key(k): [norm_key(g) for g in v] for k, v in (cfg.get("guards") or {}).items()}
        self._hangul_keys = sorted((k for k in self.keys if _HANGUL.search(k)), key=len, reverse=True)
        self._english_keys = {k for k in self.keys if not _HANGUL.search(k)}

    # ---------- 토큰 → 키 ----------
    def _guarded(self, key: str, token_norm: str) -> bool:
        return any(g in token_norm for g in self.guards.get(key, ()))

    def _token_hits(self, tok: str) -> list[tuple[int, int, str]]:
        """토큰 안 키 위치 [(start, end, key)] — 긴 키부터, 같은 길이면 뒤쪽 우선(한국어 핵심어는 끝), 겹치지 않게."""
        n = norm_key(tok)
        if not _HANGUL.search(n):  # 영문 토큰: 키 또는 키+s/es와 토큰 전체가 같을 때만
            for suf in _EN_SUFFIX:
                stem = n[: len(n) - len(suf)] if suf else n
                if n.endswith(suf) and stem in self._english_keys:
                    return [(0, len(n), stem)]
            return []
        occ: list[tuple[int, int, str]] = []
        for key in self._hangul_keys:
            if self._guarded(key, n):
                continue
            for m in re.finditer(re.escape(key), n):
                occ.append((m.start(), m.end(), key))
        occ.sort(key=lambda x: (-(x[1] - x[0]), -x[0]))
        chosen: list[tuple[int, int, str]] = []
        for s, e, k in occ:
            if all(e <= cs or s >= ce for cs, ce, _ in chosen):
                chosen.append((s, e, k))
        return sorted(chosen)

    def _segment_hits(self, toks: list[str]) -> list[_Hit]:
        norms = [norm_key(t) for t in toks]
        hits: list[_Hit] = []
        i = 0
        while i < len(toks):
            # 이웃 두 토큰을 이은 값이 키와 같고 어느 한 토큰만으로는 그 키가 안 되면 한 덩어리(스웻 셔츠 → 스웻셔츠)
            if i + 1 < len(toks):
                pair = norms[i] + norms[i + 1]
                if pair in self.keys and pair not in (norms[i], norms[i + 1]):
                    kind, minor, major = self.keys[pair]
                    hits.append(_Hit(kind, pair, minor, major, f"{toks[i]} {toks[i + 1]}", i, i + 1))
                    i += 2
                    continue
            th = self._token_hits(toks[i])
            for s, e, key in th:
                kind, minor, major = self.keys[key]
                raw = toks[i] if len(th) == 1 else norms[i][s:e]
                hits.append(_Hit(kind, key, minor, major, raw, i, i))
            i += 1
        return hits

    # ---------- 표지 ----------
    def _marker_tokens(self, toks: list[str]) -> list[tuple[int, str]]:
        """세트 표지가 있는 토큰 위치. 영문 표지는 토큰 전체 일치(이웃 두 토큰 포함: SET UP·two piece)."""
        norms = [norm_key(t) for t in toks]
        out: list[tuple[int, str]] = []
        for i, n in enumerate(norms):
            for m in self.markers:  # 한 토큰에 표지가 여럿일 수 있다(상하의세트 = 상하의 + 세트)
                if _HANGUL.search(m):
                    if m in n and not self._guarded(m, n):
                        out.append((i, m))
                elif n == m or (i + 1 < len(norms) and n + norms[i + 1] == m):
                    out.append((i, m))
        return out

    # ---------- 판정 ----------
    def decide(self, name: Optional[str]) -> Decision:
        name = (name or "").strip()
        if not name:
            return Decision("none")
        all_toks = tokenize(name)
        markers = self._marker_tokens(all_toks)
        has_marker = bool(markers)
        has_ul = any(m in self.ul_markers for _, m in markers)
        has_plus = "+" in re.sub(r"1\s*\+\s*1", "", name)
        pm = _PACK.search(name)
        pack_size = (int(pm.group(1)) if pm.group(1) else 2) if pm else None
        nm = _NSET.search(name)
        nset_size = int(nm.group(1)) if nm else None
        if not (has_marker or has_plus or pack_size):
            return Decision("none")

        sep = r"[+&,]" if has_marker else r"\+"
        segments = [s for s in re.split(sep, re.sub(r"1\s*\+\s*1", " ", name))]
        seg_toks = [tokenize(s) for s in segments]
        comps: list[Component] = []
        accessories: list[str] = []
        # 전역 토큰 순서(구간 토큰을 이어 붙인 위치): 표지가 구성품 사이에 있는지 볼 때 쓴다
        offset = 0
        seg_offsets: list[int] = []
        for si, toks in enumerate(seg_toks):
            seg_offsets.append(offset)
            hits = self._segment_hits(toks)
            # 수식어: 같은 구간에서 다음 키가 액세서리이거나 상분류가 다르면 이 키는 수식어
            keep: list[_Hit] = []
            for j, h in enumerate(hits):
                nxt = hits[j + 1] if j + 1 < len(hits) else None
                if h.key in self.modifiers and nxt is not None and (nxt.kind == "accessory" or nxt.major != h.major):
                    continue
                keep.append(h)
            # 같은 상분류 키가 이어지면 한 벌 (구체적인 하분류 우선, 아니면 뒤쪽 = 핵심어)
            merged: list[list[_Hit]] = []
            for h in keep:
                if h.kind == "accessory":
                    accessories.append(h.raw)
                    merged.append([h])
                    continue
                prev = merged[-1] if merged else None
                if prev and prev[-1].kind != "accessory" and prev[-1].major == h.major:
                    prev.append(h)
                else:
                    merged.append([h])
            for grp in merged:
                if grp[0].kind == "accessory":
                    continue
                best = grp[-1]
                for h in grp:
                    if best.minor is None and h.minor is not None:
                        best = h
                    elif best.minor in self.generic and h.minor is not None and h.minor not in self.generic:
                        best = h
                start, end = grp[0].tok_start, grp[-1].tok_end
                raw = " ".join(dict.fromkeys(h.raw for h in grp))
                comps.append(Component(best.minor, best.major, raw, si, start, end))
            offset += len(toks)

        if has_ul:
            have = {c.category_major for c in comps}
            for major in (V.CategoryMajor.UPPER.value, V.CategoryMajor.LOWER.value):
                if major not in have:
                    comps.append(Component(None, major, "상하의", len(segments) - 1, -1, -1))
        d = Decision("none", components=comps, accessories=accessories, markers=[m for _, m in markers])
        kinds = d.kinds
        n_segments = len({c.segment for c in comps})

        if pack_size:
            if len(kinds) <= 1:
                return self._fill(Decision("multipack", "pack", comps, accessories, pack_size, d.markers), seg_toks)
            return self._fill(Decision("candidate", "pack_multi_kind", comps, accessories, pack_size, d.markers), seg_toks)
        norm_name = norm_key(name)
        tok_norms = {norm_key(t) for t in all_toks}
        if has_marker and (any(m in norm_name for m in self.choice_markers) or tok_norms & self.choice_tokens):
            return self._fill(Decision("candidate", "choice", comps, accessories, None, d.markers), seg_toks)
        if len(comps) >= 2 and len(kinds) == 1:
            return self._fill(Decision("multipack", "same_kind_repeated", comps, accessories, len(comps), d.markers), seg_toks)
        if nset_size and len(kinds) <= 1:
            return self._fill(Decision("multipack", "n_kind_set", comps, accessories, nset_size, d.markers), seg_toks)
        if len(kinds) >= 2:
            if n_segments == 1 and has_marker and not has_ul:
                first = seg_offsets[comps[0].segment] + comps[0].tok_start
                last = seg_offsets[comps[-1].segment] + comps[-1].tok_end
                if any(first < pos < last for pos, _ in markers):
                    return self._fill(Decision("candidate", "marker_between_components", comps, accessories, None, d.markers), seg_toks)
            if n_segments >= 2 or has_marker:
                return self._fill(Decision("split", "name_rule", comps, accessories, None, d.markers), seg_toks)
        if len(kinds) == 1 and accessories:
            return self._fill(Decision("accessory_set", "garment_plus_accessory", comps, accessories, None, d.markers), seg_toks)
        if has_marker:
            return self._fill(Decision("candidate", "marker_single_kind", comps, accessories, None, d.markers), seg_toks)
        return Decision("none", components=comps, accessories=accessories)

    @staticmethod
    def _fill(d: Decision, seg_toks: list[list[str]]) -> Decision:
        """구성품마다 속성을 찾을 원문 범위: 같은 구간의 앞 구성품 다음부터 자기 끝까지(구간 마지막 구성품은 구간 끝까지)."""
        by_seg: dict[int, list[Component]] = {}
        for c in d.components:
            if c.tok_start >= 0:
                by_seg.setdefault(c.segment, []).append(c)
        for seg, cs in by_seg.items():
            toks = seg_toks[seg]
            for i, c in enumerate(cs):
                start = cs[i - 1].tok_end + 1 if i else 0
                end = c.tok_end + 1 if i + 1 < len(cs) else len(toks)
                c.text = " ".join(toks[start:end])
        return d


# ---------- 검수 덮어쓰기 ----------
class SetOverrides:
    """사람이 채택한 검수 결과(product_id 기준). 파일이 없으면 비어 있다."""

    def __init__(self, path: Path | str = DEFAULT_DIR / "set_overrides.yaml"):
        path = Path(path)
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.split: dict[str, list[Component]] = {}
        for pid, spec in (cfg.get("split") or {}).items():
            comps = []
            for c in spec["components"]:
                major = V.CategoryMajor(c["category_major"]).value
                minor = V.CategoryMinor(c["category_minor"]).value if c.get("category_minor") else None
                if minor and V.MINOR_TO_MAJOR[V.CategoryMinor(minor)].value != major:
                    raise ValueError(f"set_overrides: {pid} 구성품 {minor}의 상분류가 {major}가 아니다")
                comps.append(Component(minor, major, c.get("raw") or "", segment=0, tok_start=-1, tok_end=-1))
            if len({c.category_minor or c.category_major for c in comps}) < 2:
                raise ValueError(f"set_overrides: {pid} 구성품 종류가 2개 미만")
            self.split[str(pid)] = comps
        self.clear: set[str] = {str(k) for k in (cfg.get("clear_candidate") or {})}

    def apply(self, product_id: str, d: Decision) -> Decision:
        if product_id in self.split:
            comps = [Component(c.category_minor, c.category_major, c.raw, c.segment, c.tok_start, c.tok_end)
                     for c in self.split[product_id]]
            return Decision("split", "review", comps, d.accessories, None, d.markers)
        if product_id in self.clear and d.decision == "candidate":
            return Decision("none", "review_single", d.components, d.accessories, d.pack_size, d.markers)
        return d


def decision_source(d: Decision) -> str:
    return "review" if d.reason in ("review", "review_single") else "rule"


# ---------- Product 생성 ----------
def apply_split(parent: ProductRecord, d: Decision, normalizer: Any, row: dict[str, Any]) -> list[ProductRecord]:
    """판정 결과를 Product로. split → 구성품 Product들(부모는 본문에서 빠진다), multipack·candidate → 부모에 표시만,
    accessory_set·none → 부모 그대로. normalizer는 OtpenNormalizer(텍스트 속성·소재 계산을 재사용)."""
    if d.decision == "multipack":
        parent.node.multipack = True
        return [parent]
    if d.decision == "candidate":
        parent.node.split_candidate = True
        return [parent]
    if d.decision != "split":
        return [parent]
    p = parent.node
    n = len(d.components)
    reviewed = d.reason == "review"
    out: list[ProductRecord] = []
    for k, c in enumerate(d.components, start=1):
        same_major = parent.category_major is not None and parent.category_major.value == c.category_major
        if reviewed:  # 구성품별 이름 구간이 없다: 패턴만 부모(셋업은 같은 원단), 디테일·세이프는 귀속 불명이라 붙이지 않는다
            patterns, details, shape_raw, shape, shape_approx = [x.model_copy() for x in parent.patterns], [], None, None, False
        else:
            patterns, details, shape_raw, shape, shape_approx = normalizer.text_attributes([c.text], c.category_minor)
        # 소재 필드에서 온 디테일(퀼팅 등)은 부모와 공유
        for det in normalizer.material_details(row):
            if not any(x.value == det.value and x.type == det.type for x in details):
                details.append(det)
        approx = [f for f in p.approx if f in ("length_body", "length_sleeve")] if same_major else []
        if shape_approx:
            approx.append("shape")
        node = ProductNode(
            id=f"{p.id}#{k}",
            name=p.name,
            image_url=p.image_url,
            source=p.source,
            kind=p.kind,
            gender=p.gender,
            season=p.season,
            category_raw=c.raw,
            category_minor=c.category_minor,
            length_body_raw=p.length_body_raw if same_major else None,
            length_body=p.length_body if same_major else None,
            length_sleeve_raw=p.length_sleeve_raw if same_major else None,
            length_sleeve=p.length_sleeve if same_major else None,
            shape_raw=shape_raw,
            shape=shape,
            source_row=f"{p.source_row} part {k}/{n}",
            approx=approx,
            image_scope="full_set",
            parent_id=p.id,
            split_from="review" if reviewed else "name_rule",
            dup_of=f"{p.dup_of}#{k}" if p.dup_of else None,
        )
        out.append(ProductRecord(
            node=node,
            category_major=c.category_major,
            colors=[x.model_copy() for x in parent.colors],
            materials=[x.model_copy() for x in parent.materials],
            patterns=patterns,
            details=details,
        ))
    return out


def review_row(rec_id: str, name: str, sub_category: str, parent_major: Optional[str], d: Decision) -> dict[str, Any]:
    """otpensource_split_review.csv 한 행."""
    comps = " | ".join(f"{c.category_minor or '-'}/{c.category_major}:{c.raw}" for c in d.components)
    mismatch = d.decision == "split" and parent_major is not None and parent_major not in {c.category_major for c in d.components}
    return {
        "id": rec_id, "name": name, "sub_category": sub_category, "decision": d.decision, "reason": d.reason,
        "decision_source": decision_source(d),
        "components": comps, "accessories": " | ".join(d.accessories), "pack_size": d.pack_size or "",
        "markers": " | ".join(d.markers), "parent_major": parent_major or "", "parent_major_mismatch": mismatch,
    }
