"""Embedding-only text rules and readiness inventory. Source records stay untouched."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import unicodedata
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from trend_agent.schemas.graph_schema import normalize_term, term_id

TEXT_RULES_VERSION = "c1-v2"
LABELS = ("product", "item", "style", "term")
DEFAULT_PROTECTED = frozenset((
    "와이드", "오버핏", "크롭", "빈티지", "셋업", "투피스", "오버사이즈", "오버 사이즈",
    "빅사이즈", "wide", "oversize", "oversized", "crop", "cropped", "vintage", "set", "two-piece",
))
RULE_NAMES = ("unicode_nfkc", "html_unescape", "html_tags", "symbols_removed", "whitespace", "promotion",
              "size_option", "product_code", "duplicate_color", "separators")
_BRACKETS = re.compile(r"\[([^\[\]]*)\]|\(([^()]*)\)|\{([^{}]*)\}")
_PROMO = re.compile(
    r"\d+\s*%\s*(?:적립금|할인|쿠폰|세일)|(?:적립금|포인트|사은품|증정)(?:\s*\d+\s*%?)?|"
    r"무료\s*(?:배송|반품|교환)|당일\s*(?:배송|발송|출고)|오늘\s*(?:출발|배송)|빠른\s*배송|"
    r"(?:무신사\s*)?단독(?:판매|특가)?|한정\s*(?:판매|특가)|기간\s*한정|예약\s*(?:배송|발송)|"
    r"(?:신상|특가|할인|쿠폰|세일|인기|추천)(?:\s*\d+\s*%?)?|\b(?:SALE|HOT|BEST)\b", re.I)
_SIZE = r"(?:[2-6]?X{0,3}[SML]|FREE|ONE\s*SIZE|FREESIZE)"
_SIZE_LIST = re.compile(rf"(?<![\w/]){_SIZE}(?:\s*[/,~\-]\s*{_SIZE})+(?![\w/])", re.I)
_SINGLE_SIZE = re.compile(r"(?<![\w/.'’])(?:(?i:FREE(?:SIZE)?|ONE\s*SIZE|[2-6]?X{1,3}[SL])|[SML])(?![\w/.'’])")
_SIZE_LABEL = re.compile(r"(?<!\w)(?:사이즈|SIZE)(?![가-힣A-Za-z])\s*[:=]?\s*(?:\d{2,3}(?:\s*[/,~\-]\s*\d{2,3})*)?", re.I)
_CODE = re.compile(r"(?<!\w)[A-Za-z0-9][A-Za-z0-9._/\-]*(?!\w)")
_COLOR_GROUPS = {
    "black": ("블랙", "검정", "black", "blk", "bk"), "white": ("화이트", "흰색", "white", "wht", "wh"),
    "ivory": ("아이보리", "ivory", "iv"), "gray": ("그레이", "회색", "gray", "grey", "gry", "gy"),
    "navy": ("네이비", "navy", "nvy", "nv"), "blue": ("블루", "파랑", "blue", "blu", "bl"),
    "brown": ("브라운", "갈색", "brown", "brn", "br"), "beige": ("베이지", "beige", "beg", "be"),
    "green": ("그린", "초록", "green", "grn", "gn"), "red": ("레드", "빨강", "red", "rd"),
    "pink": ("핑크", "pink", "pnk", "pk"), "purple": ("퍼플", "보라", "purple", "ppl", "pu"),
    "yellow": ("옐로우", "노랑", "yellow", "ylw", "yl"),
}
_COLOR_LOOKUP = {alias: key for key, values in _COLOR_GROUPS.items() for alias in values}
_COLOR = re.compile(r"(?<!\w)(?:" + "|".join(sorted(_COLOR_LOOKUP, key=len, reverse=True)) + r")(?!\w)", re.I)


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PreprocessAudit:
    """Count changed fields/removals separately from affected product rows."""
    def __init__(self, protected=()):
        self.protected = DEFAULT_PROTECTED | frozenset(x for x in protected if x)
        self.counts = Counter({name: 0 for name in RULE_NAMES})
        self.product_counts = Counter({name: 0 for name in RULE_NAMES})
        self.samples, self.semantic_losses = [], []

    def hit(self, rule, count=1):
        self.counts[rule] += count

    def record_product(self, node_id, before, after, text, previous, component=None):
        rules = [k for k in RULE_NAMES if self.counts[k] > previous[k]]
        self.product_counts.update(rules)
        lost = sorted(protected_matches(clean(before), self.protected) - protected_matches(after, self.protected))
        if lost:
            self.semantic_losses.append({"id": node_id, "lost": lost})
        self.samples.append({"id": node_id, "before": str(before or ""), "after": after, "text": text,
                             "rules": rules, "component": component})

    def as_dict(self):
        chosen, signatures = [], set()
        for component in ("name_rule", "review"):
            match = next((r for r in self.samples if r["component"] == component), None)
            if match:
                chosen.append(match)
        for row in self.samples:
            signature = tuple(row["rules"])
            if row["rules"] and signature not in signatures and not any(r["id"] == row["id"] for r in chosen):
                chosen.append(row)
                signatures.add(signature)
            if len(chosen) == 30:
                break
        ids = {r["id"] for r in chosen}
        for row in sorted(self.samples, key=lambda r: not bool(r["rules"])):
            if len(chosen) == 30:
                break
            if row["id"] not in ids:
                chosen.append(row)
                ids.add(row["id"])
        return {"rule_applications": dict(self.counts), "affected_products": dict(self.product_counts),
                "protected_count": len(self.protected), "semantic_losses": self.semantic_losses, "samples": chosen}


def clean(value, audit=None) -> str:
    if value is None:
        return ""
    text = str(value)
    for rule, transform in (
        ("html_unescape", html.unescape),
        ("html_tags", lambda s: re.sub(r"</?(?:br|p|div|span|b|i)\b[^>]*>", " ", s, flags=re.I)),
        ("symbols_removed", lambda s: "".join(
            " " if unicodedata.category(c) in {"So", "Sk", "Cf"} or c in "\ufe0e\ufe0f\u20e3•※" else c
            for c in re.sub(r"[0-9#*]\ufe0f?\u20e3", " ", s))),
        ("unicode_nfkc", lambda s: unicodedata.normalize("NFKC", s)),
        ("whitespace", lambda s: " ".join(s.split())),
    ):
        result = transform(text)
        if result != text and audit:
            audit.hit(rule)
        text = result
    return text


@lru_cache(maxsize=4096)
def _protected_pattern(word):
    return re.compile((r"(?<![a-z])" + re.escape(word.casefold()) + r"(?![a-z])")
                      if word.isascii() else re.escape(word.casefold()))


def protected_matches(text, protected):
    if not text:
        return set()
    folded = text.casefold()
    return {word for word in protected if _protected_pattern(word).search(folded)}


def product_name(value, *, audit=None, protected=()) -> str:
    """Only the embedding name changes; never mutate ProductRecord."""
    words = audit.protected if audit else DEFAULT_PROTECTED | frozenset(protected)
    text = clean(value, audit)

    def hit(rule, count=1):
        if audit and count:
            audit.hit(rule, count)

    def promo(match):
        inner = next(g for g in match.groups() if g is not None)
        result, count = _PROMO.subn(" ", inner)
        hit("promotion", count)
        return " " + result.strip(" /,;:-_!%") + " " if count else match[0]

    text = _BRACKETS.sub(promo, text)

    def sizes(match):
        # L/S and S/S can mean sleeve length; increasing sequences are options.
        sizes = re.split(r"\s*[/,~\-]\s*", match[0].upper())
        order = {x: i for i, x in enumerate(("XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL", "5XL", "6XL"))}
        ranks = [order.get(x, -1) for x in sizes]
        if not protected_matches(match[0], words) and all(n >= 0 for n in ranks) and all(a < b for a, b in zip(ranks, ranks[1:])):
            hit("size_option")
            return " "
        return match[0]

    text = _SIZE_LIST.sub(sizes, text)
    for pattern in (_SINGLE_SIZE, _SIZE_LABEL):
        def option(match):
            if any(p.start() < match.end() and p.end() > match.start()
                   for word in words for p in _protected_pattern(word).finditer(text.casefold())):
                return match[0]
            hit("size_option")
            return " "
        text = pattern.sub(option, text)

    def erase_code(token):
        if sum(c.isdigit() for c in token) < 4 or protected_matches(token, words):
            return token
        if "_" in token:
            return " ".join(erase_code(part) for part in token.split("_") if part)
        if re.fullmatch(r"\d+(?:g|gsm|mm|cm|fill)", token, re.I):
            return token
        hit("product_code")
        # A color attached to a SKU is still garment meaning (e.g. A13HD571BLACK).
        color = re.search(r"(?:BLACK|WHITE|IVORY|GREY|GRAY|NAVY|BLUE|BROWN|BEIGE|GREEN|RED|PINK|PURPLE|YELLOW)$", token, re.I)
        if color:
            return " " + color[0] + " "
        return " "

    text = _CODE.sub(lambda match: erase_code(match[0]), text)
    if "_" in text:
        hit("separators")
        text = text.replace("_", " ")
    colors = defaultdict(list)
    for match in _COLOR.finditer(text):
        colors[_COLOR_LOOKUP[match[0].lower()]].append(match)
    drops = set()
    for matches in colors.values():
        if len(matches) > 1:
            full = [m for m in matches if len(m[0]) > 3 or not m[0].isascii()]
            keep = (full or matches)[0]
            drops.update(m.span() for m in matches if m.span() != keep.span())
    for start, end in sorted(drops, reverse=True):
        if not protected_matches(text[start:end], words):
            text = text[:start] + " " + text[end:]
            hit("duplicate_color")
    before = text
    text = re.sub(r"\[\s*\]|\(\s*\)|\{\s*\}", " ", text)
    text = re.sub(r"\s+[/,:;\-]+(?=\s|$)|^\s*[/,:;\-]+\s*", " ", text)
    text = " ".join(text.split()).strip(" /,:;-_")
    if text != before:
        hit("separators")
    return text


def _values(values, *, detail=False, audit=None) -> str:
    result = set()
    for entry in values or []:
        if isinstance(entry, dict):
            value = clean(entry.get("value"), audit)
            if value and detail and entry.get("type"):
                value = f"{clean(entry['type'], audit)}:{value}"
            if value and entry.get("approx"):
                value = "~" + value
        else:
            value = clean(entry, audit)
        if value:
            result.add(value)
    return ", ".join(sorted(result))


def garment_text(record: dict, *, item=False, audit=None) -> str:
    node = record.get("node", record)
    approx = set(node.get("approx") or [])

    def scalar(key):
        value = clean(node.get(key), audit)
        return ("~" if key in approx else "") + value if value else ""

    parts = []
    category = ">".join(v for v in (clean(record.get("category_major"), audit), scalar("category_minor")) if v)
    if category:
        parts.append(category)
    for key, label in (("colors", "색"), ("materials", "소재"), ("patterns", "패턴"), ("details", "디테일")):
        value = _values(record.get(key), detail=key == "details", audit=audit)
        if value:
            parts.append(f"{label} {value}")
    if scalar("fit"):
        parts.append("핏 " + scalar("fit"))
    body, sleeve = scalar("length_body"), scalar("length_sleeve")
    if body or sleeve:
        parts.append(f"기장 {body}/{sleeve}")
    name = scalar("name") if item else product_name(node.get("name"), audit=audit)
    if name:
        parts.append(name)
    if not item and (node.get("parent_id") or node.get("split_from")) and scalar("category_minor"):
        parts.append("세트 구성품 " + scalar("category_minor"))
    if item and scalar("styling_raw"):
        parts.append(scalar("styling_raw"))
    return " | ".join(parts)


def style_text(style: dict, *, audit=None) -> str:
    parts = [clean(style.get("name"), audit)]
    aliases = style.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    parts.extend(sorted({clean(x, audit) for x in aliases} - {""}))
    parts.append(clean(style.get("definition"), audit))
    for desc in style.get("description") or []:
        parts.append(clean(desc.get("text"), audit) if isinstance(desc, dict) else clean(desc, audit))
    return " | ".join(p for p in parts if p)


def _natural(path: Path):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", path.name)]


def input_plan(shared: Path, *, product_version=None, extraction_version=None):
    """Require upstream handoff and data. v1 is explicit historical replay only."""
    handoffs = shared.parent / "_vault/00_Team/handoffs"
    version = product_version or "v2"
    candidates = sorted((shared / "data/processed" / version).glob("*.jsonl"))
    products = [p for p in candidates if p.name in {"musinsa_snap_all.jsonl", "otpensource_all.jsonl"}]
    products = products or [p for p in candidates if not p.name.startswith("otpensource_split_")]
    ph, eh = handoffs / "T2-T3-products-v2.md", handoffs / "T1-T3,T4-extractions-ready.md"
    extroot, pv = shared / "extractions", extraction_version
    if pv is None and eh.exists():
        declared = re.search(r"^version:\s*(extract-[\w.\-]+)", eh.read_text(), re.M)
        if declared:
            pv = declared[1]
        else:
            versions = sorted((p for p in extroot.glob("extract-*") if p.is_dir() and list(p.glob("*.json"))), key=_natural)
            pv = versions[-1].name if versions else None
    extractions = sorted((extroot / pv).glob("*.json")) if pv else []
    termmap = shared / "terms/term_map.jsonl"
    ready = {"products_v2": bool(products and (ph.exists() or version == "v1")),
             "extractions": bool(eh.exists() and extractions), "term_map": termmap.exists()}
    return {"ready": ready, "pending": [k for k, ok in ready.items() if not ok],
            "product_version": version if ready["products_v2"] else None,
            "extraction_version": pv if ready["extractions"] else None,
            "products": products if ready["products_v2"] else [], "extractions": extractions if ready["extractions"] else [],
            "handoffs": [p for p, ok in ((ph, ready["products_v2"] and version != "v1"), (eh, ready["extractions"])) if ok],
            "termmap": termmap if ready["term_map"] else None}


def collect_inputs(shared: Path, *, product_version=None, extraction_version=None):
    """Only ready, graph-scoped rows; labels override extraction per article."""
    plan = input_plan(shared, product_version=product_version, extraction_version=extraction_version)
    rows, sources, stats = {label: {} for label in LABELS}, [], Counter()

    def source(path):
        raw = path.read_bytes()
        sources.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        return raw

    for path in plan["handoffs"]:
        source(path)
    keywords, protected = shared / "keywords/inventory.csv", []
    if keywords.exists():
        reader = csv.DictReader(io.StringIO(source(keywords).decode("utf-8-sig")))
        field = next((k for k in ("text", "keyword", "term") if k in (reader.fieldnames or [])), None)
        if field is None:
            raise ValueError(f"{keywords}: requires text, keyword or term column")
        protected = [clean(record[field]) for record in reader if record[field]]
    audit = PreprocessAudit(protected)

    def add(label, node_id, text):
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError(f"Missing {label} id")
        if not text:
            raise ValueError(f"Empty {label} embedding text: {node_id}")
        sha, old = text_sha(text), rows[label].get(node_id)
        if old and old["text_sha"] != sha:
            raise ValueError(f"Conflicting {label} id: {node_id}")
        if old:
            stats[label + "_duplicate_rows"] += 1
        rows[label][node_id] = {"id": node_id, "text_sha": sha, "text": text}

    for path in plan["products"]:
        for lineno, line in enumerate(source(path).decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                node = record.get("node", record)
                stats["product_input_rows"] += 1
                if record.get("category_major") is None:
                    stats["product_out_of_scope_skipped"] += 1
                    continue
                previous = audit.counts.copy()
                text = garment_text(record, audit=audit)
                add("product", node.get("id"), text)
                audit.record_product(node["id"], node.get("name"), product_name(node.get("name"), protected=audit.protected),
                                     text, previous, node.get("split_from"))
                if node.get("parent_id") or node.get("split_from"):
                    stats["product_set_components"] += 1
            except Exception as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc

    articles, prompt_versions = {}, set()
    groups = [plan["extractions"], sorted((shared / "labels").glob("*.json")) if plan["ready"]["extractions"] else []]
    for group_idx, paths in enumerate(groups):
        seen = set()
        for path in paths:
            doc = json.loads(source(path))
            ext = doc.get("extraction", doc)
            if not isinstance(ext, dict) or "styles" not in ext or "article_id" not in ext:
                stats["non_extraction_json_skipped"] += 1
                continue
            aid = str(ext["article_id"])
            if aid in seen:
                raise ValueError(f"Duplicate extraction article in same source group: {aid}")
            seen.add(aid)
            if group_idx == 0 and doc.get("label_meta", {}).get("prompt_version"):
                prompt_versions.add(doc["label_meta"]["prompt_version"])
            if aid in articles:
                stats["label_overrides_extraction"] += 1
            articles[aid] = (ext, "label" if group_idx else "extraction")
    aliases, term_candidates = None, defaultdict(list)
    for aid, (ext, origin) in sorted(articles.items()):
        for si, style in enumerate(ext["styles"]):
            sid = f"{aid}-s{si}"
            stats["style_input_rows"] += 1
            add("style", sid, style_text(style, audit=audit))
            for alias in style.get("aliases") or []:
                norm = normalize_term(alias)
                if norm and norm != normalize_term(style["name"]):
                    term_candidates[norm].append({"text": alias, "kind": "alias", "origin": origin})
            for ii, item in enumerate(style.get("items") or []):
                stats["item_input_rows"] += 1
                if item.get("category_major") is None:
                    stats["item_out_of_scope_skipped"] += 1
                    continue
                if item.get("fit_raw") and item.get("fit"):
                    from trend_agent.ingestion.normalization.alias_map import AliasMap
                    aliases = aliases or AliasMap()
                    normalized = aliases.normalize("fit", item["fit_raw"])
                    if normalized.approx and normalized.value == item["fit"]:
                        item = {**item, "approx": sorted(set(item.get("approx") or []) | {"fit"})}
                add("item", f"{sid}-i{ii}", garment_text(item, item=True, audit=audit))

    if plan["termmap"]:
        for lineno, line in enumerate(source(plan["termmap"]).decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            value = entry.get("text", entry.get("term"))
            norm = normalize_term(value or "")
            if not norm or (entry.get("norm") and entry["norm"] != norm) or (entry.get("id") and entry["id"] != term_id(norm)):
                raise ValueError(f"{plan['termmap']}:{lineno}: Term id/norm differs from contract normalization")
            term_candidates[norm].append({**entry, "text": value})
            stats["term_map_rows"] += 1
        kinds, origins = {"alias": 0, "translation": 1, "trend_word": 2}, {"label": 0, "extraction": 1, "manual": 2, "term_mentions": 3, "llm": 4}
        for norm, candidates in sorted(term_candidates.items()):
            profiles = [(i, x) for i, x in enumerate(candidates) if x.get("row") == "profile"]
            if profiles:
                latest = max(profiles, key=lambda c: (c[1].get("created_at") or "", c[0]))[1]
                if len(profiles) == len(candidates) and (latest.get("profile") or {}).get("recognized") is False:
                    stats["term_unrecognized_profile_skipped"] += 1
                    continue
            first = min(enumerate(candidates), key=lambda c: (kinds.get(c[1].get("kind"), 2), origins.get(c[1].get("origin"), 3), c[0]))[1]
            display = clean(first["text"], audit)
            surfaces = {clean(c["text"], audit) for c in candidates}
            for c in candidates:
                other = c.get("aliases") or []
                surfaces.update(clean(x, audit) for x in ([other] if isinstance(other, str) else other))
            add("term", term_id(norm), " | ".join([display, *sorted(surfaces - {display, ""})]))
        stats["term_input_unique_ids"] = len(rows["term"])
    # Keyword inventory is protection only. Style aliases wait for authoritative T3 input.
    for label, entries in rows.items():
        stats[label + "_rows"] = len(entries)
        stats[label + "_unique_texts"] = len({r["text_sha"] for r in entries.values()})
    for label in ("product", "item", "style"):
        expected = stats[label + "_input_rows"] - stats[label + "_out_of_scope_skipped"]
        if expected != stats[label + "_rows"]:
            raise ValueError(f"{label}: scoped input count {expected} != unique ids {stats[label + '_rows']}")
    if audit.semantic_losses:
        raise ValueError(f"Protected fashion meaning removed: {audit.semantic_losses[:5]}")
    metadata = {"product_version": plan["product_version"], "extraction_version": plan["extraction_version"],
                "extractions_prompt_versions": sorted(prompt_versions), "ready": plan["ready"], "pending": plan["pending"],
                "sources": sources, "statistics": dict(stats), "preprocessing": audit.as_dict()}
    for key, paths in (("v2_sha256", plan["products"]), ("extractions_sha256", plan["extractions"])):
        selected = [s for s in sources if s["path"] in {str(p) for p in paths}]
        metadata[key] = text_sha(json.dumps([(Path(x["path"]).name, x["sha256"]) for x in selected], ensure_ascii=False)) if selected else None
    metadata["term_map_sha256"] = next((x["sha256"] for x in sources if x["path"] == str(plan["termmap"])), None)
    return {k: sorted(v.values(), key=lambda r: r["id"]) for k, v in rows.items()}, metadata


def preprocess_report(metadata):
    audit = metadata["preprocessing"]
    lines = ["# C1 임베딩 입력 전처리 c1-v2", "", "원본 파일은 읽기만 한다. 아래 문장/이름만 모델에 입력한다. 키워드 행렬은 보호어이며 Term 입력이 아니다.",
             f"보호어 {audit['protected_count']}개 · 의미어 손실 {len(audit['semantic_losses'])}건 · pending: {metadata['pending']}", "",
             "적용 건수는 공통 규칙의 변경 필드 호출 수, 이름 규칙의 제거 횟수다. 상품 수는 해당 규칙이 적용된 상품 행 수다.", "",
             "| 규칙 | 적용 건수 | 적용 상품 수 |", "|---|---:|---:|"]
    for rule, count in audit["rule_applications"].items():
        lines.append(f"| {rule} | {count} | {audit['affected_products'][rule]} |")
    lines += ["", "## 정제 전·후 표본 (최대 30개, 규칙 조합 우선)", "", "| ID | 원문 상품명 | 임베딩 이름 | 적용 규칙 | 최종 입력 문장 |", "|---|---|---|---|---|"]
    def esc(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    for r in audit["samples"]:
        lines.append("| " + " | ".join(esc(r[k]) for k in ("id", "before", "after", "rules", "text")) + " |")
    lines += ["", "의미어 검증: 기본 패션 보호어 + inventory의 각 원문 표기가 정제 전·후에 남는지 검사한다. 손실 발생 시 게시 전에 실패한다.",
              "이 자동 검사는 전체 의미의 동등성을 보장하지 않는다. 표본은 이름·구성품·정규화 속성을 함께 검토한다."]
    return "\n".join(lines) + "\n"
