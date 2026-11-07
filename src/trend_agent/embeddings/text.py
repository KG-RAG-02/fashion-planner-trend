"""Canonical node text and input inventory; no model imports or network calls."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

TEXT_RULES_VERSION = "c1-v1"
LABELS = ("product", "item", "style", "term")


def clean(value) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value)).split()) if value is not None else ""


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _values(values, *, detail=False) -> str:
    result = set()
    for entry in values or []:
        if isinstance(entry, dict):
            value = clean(entry.get("value"))
            if value and detail and entry.get("type"):
                value = f"{clean(entry['type'])}:{value}"
            if value and entry.get("approx"):
                value = "~" + value
        else:
            value = clean(entry)
        if value:
            result.add(value)
    return ", ".join(sorted(result))


def garment_text(record: dict, *, item=False) -> str:
    node = record.get("node", record)
    approx = set(node.get("approx") or [])

    def scalar(key):
        value = clean(node.get(key))
        return ("~" if key in approx else "") + value if value else ""

    parts = []
    category = ">".join(v for v in (clean(record.get("category_major")), scalar("category_minor")) if v)
    if category:
        parts.append(category)
    for key, label in (("colors", "색"), ("materials", "소재"), ("patterns", "패턴"), ("details", "디테일")):
        value = _values(record.get(key), detail=key == "details")
        if value:
            parts.append(f"{label} {value}")
    if scalar("fit"):
        parts.append("핏 " + scalar("fit"))
    body, sleeve = scalar("length_body"), scalar("length_sleeve")
    if body or sleeve:
        parts.append(f"기장 {body}/{sleeve}")
    if scalar("name"):
        parts.append(scalar("name"))
    if item and scalar("styling_raw"):
        parts.append(scalar("styling_raw"))
    return " | ".join(parts)


def style_text(style: dict) -> str:
    parts = [clean(style.get("name"))]
    aliases = style.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    parts.extend(sorted(set(clean(x) for x in aliases if clean(x))))
    parts.append(clean(style.get("definition")))
    for desc in style.get("description") or []:
        parts.append(clean(desc.get("text")) if isinstance(desc, dict) else clean(desc))
    return " | ".join(p for p in parts if p)


def _natural(path: Path):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", path.name)]


def collect_inputs(shared: Path, *, product_version=None, extraction_version=None):
    """Return rows, source metadata, counts. Labels override extraction per article."""
    version = product_version or ("v2" if list((shared / "data/processed/v2").glob("*.jsonl")) else "v1")
    products = sorted((shared / "data/processed" / version).glob("*.jsonl"))
    if not products:
        raise FileNotFoundError(f"No Product JSONL in {shared / 'data/processed' / version}")
    rows = {label: {} for label in LABELS}
    sources = []
    stats = Counter()

    def source(path):
        raw = path.read_bytes()
        sources.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        return raw

    def add(label, node_id, text):
        if not text:
            stats[label + "_empty_text_skipped"] += 1
            return
        node_id = clean(node_id)
        if not node_id:
            raise ValueError(f"Missing {label} id")
        sha = text_sha(text)
        old = rows[label].get(node_id)
        if old and old["text_sha"] != sha:
            raise ValueError(f"Conflicting {label} id: {node_id}")
        rows[label][node_id] = {"id": node_id, "text_sha": sha, "text": text}

    for path in products:
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
                add("product", node.get("id"), garment_text(record))
            except Exception as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc

    extroot = shared / "extractions"
    versions = sorted((p for p in extroot.glob("*") if p.is_dir() and list(p.glob("*.json"))), key=_natural)
    pv = extraction_version or (versions[-1].name if versions else None)
    articles = {}
    groups = [sorted((extroot / pv).glob("*.json")) if pv else [], sorted((shared / "labels").glob("*.json"))]
    for paths in groups:
        seen = set()
        for path in paths:
            doc = json.loads(source(path))
            ext = doc.get("extraction", doc)
            if not isinstance(ext, dict) or "styles" not in ext or "article_id" not in ext:
                stats["non_extraction_json_skipped"] += 1
                continue
            aid = clean(ext["article_id"])
            if aid in seen:
                raise ValueError(f"Duplicate extraction article in same source group: {aid}")
            seen.add(aid)
            if aid in articles:
                stats["label_overrides_extraction"] += 1
            articles[aid] = ext
    aliases = None
    for aid, ext in sorted(articles.items()):
        for si, style in enumerate(ext["styles"]):
            sid = f"{aid}-s{si}"
            add("style", sid, style_text(style))
            for ii, item in enumerate(style.get("items") or []):
                if item.get("category_major") is None:
                    stats["item_out_of_scope_skipped"] += 1
                    continue
                # Match graph.loader.mark_item_approx without importing Neo4j.
                if item.get("fit_raw") and item.get("fit"):
                    from trend_agent.ingestion.normalization.alias_map import AliasMap
                    aliases = aliases or AliasMap()
                    normalized = aliases.normalize("fit", item["fit_raw"])
                    if normalized.approx and normalized.value == item["fit"]:
                        item = {**item, "approx": sorted(set(item.get("approx") or []) | {"fit"})}
                add("item", f"{sid}-i{ii}", garment_text(item, item=True))

    termmap = shared / "terms/term_map.jsonl"
    if termmap.exists():
        for lineno, line in enumerate(source(termmap).decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            term = json.loads(line)
            value = term.get("text", term.get("term"))
            node_id = term.get("id") or ("term:" + clean(term["norm"]) if term.get("norm") else None)
            if value is None or node_id is None:
                raise ValueError(f"{termmap}:{lineno}: Term requires id/text or T3 norm/term fields")
            add("term", node_id, clean(value))
            stats["term_map_rows"] += 1
    keywords = shared / "keywords/inventory.csv"
    if keywords.exists():
        import io
        reader = csv.DictReader(io.StringIO(source(keywords).decode("utf-8-sig")))
        field = next((k for k in ("text", "keyword", "term") if k in (reader.fieldnames or [])), None)
        if field is None:
            raise ValueError(f"{keywords}: requires text, keyword or term column")
        existing_terms = {r["text"] for r in rows["term"].values()}
        for record in reader:
            text = clean(record[field])
            if text and text not in existing_terms:
                node_id = record.get("id") or ("term:" + clean(record["norm"]) if record.get("norm") else None)
                if not node_id:
                    node_id = "keyword:" + text_sha(text)
                    stats["keyword_synthetic_ids"] += 1
                add("term", node_id, text)
                existing_terms.add(text)
    for label, entries in rows.items():
        stats[label + "_rows"] = len(entries)
        stats[label + "_unique_texts"] = len({r["text_sha"] for r in entries.values()})
    metadata = {"product_version": version, "extraction_version": pv, "sources": sources, "statistics": dict(stats)}
    return {k: sorted(v.values(), key=lambda r: r["id"]) for k, v in rows.items()}, metadata
