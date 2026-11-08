"""결과 JSON(계약 v1.1) → Obsidian 볼트 노트·Base·Canvas (T2 작업 5 · 설계 메모 §3.6).

실행:  python scripts/export_obsidian.py RESULT.json [RESULT2.json …] --vault "$VAULT" [--products-dir "$SHARED/data/processed/v2"]
쓰는 곳(볼트 루트 기준):
  10_Trends/<트렌드>.md            type: trend      — 정의·근거(기사 링크만)·Item 목록
  20_Items/<Item>.md               type: item       — 본문에 속성 위키링크 → 그래프 뷰에 Item → 속성 ← Product
  30_Attributes/<kind>/<값>.md     type: attribute  — 색·소재·패턴·디테일·핏·카테고리
  40_Products/<상품>.md            type: product    — image(02 URL · 03 "[[assets/snap/<row>.jpg]]"), score(Number) …
  90_Bases/트렌드 의류.base        cards + table 뷰(공식 문서 키만 — 카드 이미지·정렬은 UI에서 설정, 레퍼런스 §2-1)
  91_Canvas/<트렌드>.canvas        JSON Canvas 1.0 (트렌드 → Item → 상품; 03은 file 노드, 02는 text 노드 외부 이미지)
  assets/snap/<row>.jpg            결과에 쓰인 03 원본만 **복사**(심볼릭 링크 아님, sha256 대조, 재인코딩 없음)
규칙
- 같은 입력이면 같은 출력: 시각 대신 입력 sha256(`source_hash`), 정렬 고정, 파일명 = slug(금지 문자 `# | ^ : %% [[ ]] / \\` 제거).
- `.md`는 frontmatter에 `generated: true`가 있는 파일만 다시 쓴다(사람이 쓴 노트는 건너뛰고 보고).
  `.base`·`.canvas`는 frontmatter가 없어서 내보내기 기록(`<vault>/90_Bases/.export_manifest.json`)의 sha와 현재 내용이 같을 때만
  다시 쓴다 — 사용자가 UI로 고친 뒤에는 덮어쓰지 않는다.
- Vogue 기사 이미지는 링크만(G0-⑤). 인용은 로컬 볼트에만 둔다(Sync 연결 금지, Q5).
- 그래프 선은 본문 위키링크로 만든다(frontmatter 링크가 그래프 선이 되는지는 미확인 — 레퍼런스 §6).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from render_trend_result import all_products, enrich_from_products, load_result, shared_dir, snap_parts  # noqa: E402

from trend_agent.schemas.trend_analysis_result import ProductMatch, TrendAnalysisResult  # noqa: E402

BASE_PATH = "90_Bases/트렌드 의류.base"
MANIFEST = "90_Bases/.export_manifest.json"
_FORBIDDEN = re.compile(r"[#|^:%\[\]/\\\"*?<>\n\r\t]+")
ATTR_KINDS = ("category", "color", "material", "pattern", "detail", "neckline", "collar", "fit")


def slugify(text: str, max_len: int = 60) -> str:
    s = _FORBIDDEN.sub(" ", text or "").strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:max_len].rstrip(" .") or "untitled"


def short_hash(text: str, n: int = 6) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def yaml_str(v: Any) -> str:
    """YAML 큰따옴표 문자열 (product_id 같은 값을 문자열로 고정)."""
    return json.dumps("" if v is None else str(v), ensure_ascii=False)


def frontmatter(d: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in d.items():
        if v is None or v == [] or v == "":
            continue
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list):
            lines.append(f"{k}:")
            lines.extend(f"  - {yaml_str(x)}" for x in v)
        else:
            lines.append(f"{k}: {yaml_str(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def link(path: str, label: Optional[str] = None) -> str:
    p = path[:-3] if path.endswith(".md") else path
    return f"[[{p}|{label}]]" if label else f"[[{p}]]"


class Writer:
    """generated 규칙을 지키는 파일 쓰기. 결과를 written/skipped/unchanged로 센다."""

    def __init__(self, vault: Path, dry_run: bool = False):
        self.vault = vault
        self.dry = dry_run
        self.report: dict[str, list[str]] = defaultdict(list)
        mp = vault / MANIFEST
        self.manifest: dict[str, str] = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}

    def _write(self, rel: str, data: bytes) -> None:
        path = self.vault / rel
        if path.exists() and path.read_bytes() == data:
            self.report["unchanged"].append(rel)
            return
        if not self.dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        self.report["written"].append(rel)

    def note(self, rel: str, text: str) -> None:
        path = self.vault / rel
        if path.exists():
            head = path.read_text(encoding="utf-8").split("\n---", 1)[0]
            if not re.search(r"^generated:\s*true\s*$", head, re.MULTILINE):
                self.report["skipped_human"].append(rel)
                return
        self._write(rel, text.encode("utf-8"))

    def managed(self, rel: str, text: str) -> None:
        """.base·.canvas: 처음 쓰거나, 마지막으로 내가 쓴 내용 그대로일 때만 다시 쓴다."""
        path = self.vault / rel
        data = text.encode("utf-8")
        if path.exists():
            cur = hashlib.sha256(path.read_bytes()).hexdigest()
            if cur != self.manifest.get(rel) and path.read_bytes() != data:
                self.report["skipped_user_edited"].append(rel)
                return
        self._write(rel, data)
        self.manifest[rel] = hashlib.sha256(data).hexdigest()

    def copy_asset(self, src: Path, rel: str) -> None:
        dst = self.vault / rel
        sha = hashlib.sha256(src.read_bytes()).hexdigest()
        if dst.exists() and hashlib.sha256(dst.read_bytes()).hexdigest() == sha:
            self.report["unchanged"].append(rel)
            return
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)  # 바이트 복사(재인코딩·크롭 없음)
            if hashlib.sha256(dst.read_bytes()).hexdigest() != sha:
                raise RuntimeError(f"복사 후 sha256 불일치: {rel}")
        self.report["copied"].append(rel)

    def save_manifest(self) -> None:
        if not self.dry:
            p = self.vault / MANIFEST
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(dict(sorted(self.manifest.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ---------- 모으기 ----------
def collect(results: list[tuple[TrendAnalysisResult, str]]) -> dict[str, Any]:
    trends: dict[str, dict[str, Any]] = {}
    items: dict[str, dict[str, Any]] = {}
    products: dict[str, dict[str, Any]] = {}
    evidence: dict[str, Any] = {}
    for res, sha in results:
        for e in res.evidence:
            evidence.setdefault(e.evidence_id, e)
        mode_by_style = {sid: (r.match_mode, r.tier, r.term) for r in res.resolutions for sid in r.style_ids}
        for t in res.trends:
            st = t.style
            tr = trends.setdefault(st.id, {"style": st, "trend": t, "items": [], "requests": set(), "hashes": set(),
                                          "mode": mode_by_style.get(st.id)})
            tr["requests"].add(res.request_id)
            tr["hashes"].add(sha)
            for it in t.items:
                if it.id not in items:
                    items[it.id] = {"item": it, "trend_id": st.id, "products": []}
                    tr["items"].append(it.id)
                for p in it.similar_products:
                    pr = products.setdefault(p.product_id, {"p": p, "hits": []})
                    pr["hits"].append((p.score, st.id, it.id))
                    if p.score > pr["p"].score:
                        pr["p"] = p
                    if p.product_id not in items[it.id]["products"]:
                        items[it.id]["products"].append(p.product_id)
        for r in res.resolutions:
            for tp in r.top_products:
                pr = products.setdefault(tp.product_id, {"p": tp, "hits": []})
                for sid in r.style_ids:
                    pr["hits"].append((tp.score, sid, tp.item_id))
                pr.setdefault("ranks", []).append((r.term, tp.rank, tp.trend_score))
    return {"trends": trends, "items": items, "products": products, "evidence": evidence}


def assign_paths(data: dict[str, Any]) -> dict[str, str]:
    """id → 볼트 경로(.md). slug가 겹치면 id 해시 6자리를 붙인다(정렬 고정 → 결정적)."""
    paths: dict[str, str] = {}
    used: set[str] = set()

    def put(key: str, folder: str, base: str) -> None:
        cand = f"{folder}/{base}.md"
        if cand in used:
            cand = f"{folder}/{base} {short_hash(key)}.md"
        used.add(cand)
        paths[key] = cand

    for sid in sorted(data["trends"]):
        put(sid, "10_Trends", slugify(data["trends"][sid]["style"].name))
    for iid in sorted(data["items"]):
        put(iid, "20_Items", f"{slugify(data['items'][iid]['item'].name, 50)} ({slugify(iid, 40)})")
    for pid in sorted(data["products"]):
        p: ProductMatch = data["products"][pid]["p"]
        sp = snap_parts(pid)
        if sp:
            base = f"snap-{sp[0]:06d}-{slugify(sp[1])}" + (f"-{sp[2]}" if sp[2] else "")
        else:
            base = f"{slugify(p.name or pid, 50)} {short_hash(pid)}"
        put(pid, "40_Products", base)
    return paths


def attr_values(x: Any) -> list[tuple[str, str]]:
    """(kind, value) — 그래프 뷰용 속성 노트 목록. 근사(approx) 값도 노트는 만들되 표시에 ≈."""
    out: list[tuple[str, str]] = []
    if x.category_minor:
        out.append(("category", x.category_minor.value))
    elif x.category_major:
        out.append(("category", x.category_major.value))
    out += [("color", c.value) for c in x.colors]
    out += [("material", m) for m in x.materials]
    out += [("pattern", p) for p in x.patterns]
    out += [(d.type.value if hasattr(d.type, "value") else str(d.type), d.name) for d in x.details]  # detail·neckline·collar
    if x.fit:
        out.append(("fit", x.fit.value))
    return list(dict.fromkeys(out))


def attr_path(kind: str, value: str) -> str:
    return f"30_Attributes/{kind}/{slugify(value)}.md"


def attr_links(x: Any) -> str:
    approx = set(x.approx or [])
    by_kind: dict[str, list[str]] = defaultdict(list)
    for kind, v in attr_values(x):
        mark = " ≈" if f"{kind}:{v}" in approx else ""
        by_kind[kind].append(link(attr_path(kind, v), v) + mark)
    names = {"category": "카테고리", "color": "색", "material": "소재", "pattern": "패턴", "detail": "디테일",
             "neckline": "넥라인", "collar": "칼라", "fit": "핏"}
    return "\n".join(f"- {names.get(k, k)}: {', '.join(v)}" for k, v in by_kind.items()) or "- (속성 없음 — 미확인)"


def image_value(p: ProductMatch) -> tuple[Optional[str], Optional[int]]:
    """노트 `image` 속성 값과 03 row. 02 = URL, 03 = "[[assets/snap/<row>.jpg]]"(레퍼런스 §3·S4)."""
    if p.image_status == "broken":
        return None, None
    if p.source == "musinsa_snap":
        sp = snap_parts(p.product_id)
        row = p.snap_id if p.snap_id is not None else (sp[0] if sp else None)
        return (f"[[assets/snap/{row}.jpg]]", row) if row is not None else (None, None)
    url = p.image_url or (p.image_ref if (p.image_ref or "").startswith("http") else None)
    return url, None


# ---------- 쓰기 ----------
def export(results: list[tuple[TrendAnalysisResult, str]], vault: Path, dry_run: bool = False) -> dict[str, Any]:
    data = collect(results)
    paths = assign_paths(data)
    w = Writer(vault, dry_run)
    src_hash = short_hash("|".join(sorted(sha for _, sha in results)), 16)
    ev = data["evidence"]

    def ev_line(eid: str) -> str:
        e = ev.get(eid)
        if not e:
            return ""
        title = (e.title or e.article_id).replace("[", "(").replace("]", ")")
        return f"> “{e.quote}” — [{title}]({e.url})" if e.url else f"> “{e.quote}” — {title}"

    # 상품: 같은 사진(snap row)의 다른 결과 상품 = co_items
    by_row: dict[int, list[str]] = defaultdict(list)
    for pid in sorted(data["products"]):
        sp = snap_parts(pid)
        if sp:
            by_row[sp[0]].append(pid)

    attrs_needed: set[tuple[str, str]] = set()
    for sid in sorted(data["trends"]):
        tr = data["trends"][sid]
        st, t = tr["style"], tr["trend"]
        mode = tr["mode"] or (t.match_mode, None, t.term)
        fm = {"type": "trend", "generated": True, "source_hash": src_hash, "trend_id": st.id, "name": st.name,
              "aliases": st.aliases, "seasons": st.seasons, "match_mode": mode[0], "tier": mode[1],
              "term": mode[2] or t.term, "qualification": t.qualification, "article_id": st.article_id,
              "virtual": st.virtual or None, "requests": sorted(tr["requests"]), "tags": ["trend"]}
        body = [f"# {st.name}", "", st.definition, ""]
        for d in st.description:
            body += [d.text, ""] + [ev_line(e) for e in d.evidence_ids if ev_line(e)] + [""]
        if st.focus_core or st.focus_supporting:
            body += [f"- 핵심 속성: {', '.join(st.focus_core) or '-'}", f"- 보조 속성: {', '.join(st.focus_supporting) or '-'}", ""]
        body += ["## Item", ""] + [f"- {link(paths[iid], data['items'][iid]['item'].name)}" for iid in tr["items"]]
        body += ["", "## 상품 카드", "", f"![[{BASE_PATH}#상품 카드]]", "",
                 "> 카드 이미지·정렬은 Base 보기 설정에서 `Image property = image`, 정렬 `score` 큰 값부터로 한 번 지정한다.", ""]
        w.note(paths[sid], frontmatter(fm) + "\n".join(body) + "\n")

    for iid in sorted(data["items"]):
        rec = data["items"][iid]
        it = rec["item"]
        attrs_needed.update(attr_values(it))
        fm = {"type": "item", "generated": True, "source_hash": src_hash, "item_id": it.id, "name": it.name,
              "trend": link(paths[rec["trend_id"]]), "category_major": it.category_major and it.category_major.value,
              "category_minor": it.category_minor and it.category_minor.value, "virtual": it.virtual or None, "tags": ["item"]}
        body = [f"# {it.name}", "", f"트렌드: {link(paths[rec['trend_id']], data['trends'][rec['trend_id']]['style'].name)}", "",
                "## 속성", "", attr_links(it), ""]
        if it.styling_raw:
            body += [f"스타일링: {it.styling_raw}", ""]
        if it.image_urls:
            body += ["## Vogue 이미지 (링크만)", ""] + [f"- [기사 이미지 {i + 1}]({u})" for i, u in enumerate(it.image_urls)] + [""]
        quotes = [ev_line(e) for e in it.evidence_ids if ev_line(e)]
        if quotes:
            body += ["## 근거", ""] + quotes + [""]
        body += ["## 비슷한 상품", ""] + [f"- {link(paths[pid], data['products'][pid]['p'].name or pid)}"
                                        for pid in rec["products"]]
        w.note(paths[iid], frontmatter(fm) + "\n".join(body) + "\n")

    snaps_used: set[int] = set()
    for pid in sorted(data["products"]):
        pr = data["products"][pid]
        p: ProductMatch = pr["p"]
        attrs_needed.update(attr_values(p))
        hits = sorted(pr["hits"], key=lambda h: (-h[0], h[1], h[2]))
        best = hits[0] if hits else (p.score, None, None)
        trends = list(dict.fromkeys(link(paths[h[1]]) for h in hits if h[1] in paths))
        items = list(dict.fromkeys(link(paths[h[2]]) for h in hits if h[2] in paths))
        image, row = image_value(p)
        if row is not None:
            snaps_used.add(row)
        co = [link(paths[o]) for o in by_row.get(row, []) if o != pid] if row is not None else []
        fm = {"type": "product", "generated": True, "source_hash": src_hash, "product_id": pid, "name": p.name,
              "trend": link(paths[best[1]]) if best[1] in paths else None, "trends": trends,
              "item": link(paths[best[2]]) if best[2] in paths else None, "items": items,
              "score": round(float(best[0]), 4), "image": image, "image_scope": p.image_scope, "image_status": p.image_status,
              "has_image": bool(p.has_image), "source": p.source, "kind": p.kind, "snap_row": row, "co_items": co,
              "parent_id": p.parent_id, "multi_wearer_suspected": True if p.multi_wearer_suspected else None,
              "tags": ["product"]}
        body = [f"# {p.name or pid}", ""]
        if image and row is not None:
            body += [f"![[assets/snap/{row}.jpg|320]]", "", f"착장 원본 사진 1장(크롭 없음) — 이 의류: {snap_parts(pid)[1]}", ""]
        elif image:
            body += [f"![320]({image})", "", "세트 이미지(구성품이 부모 사진을 함께 씀)" if p.image_scope == "full_set" else "상품 컷", ""]
        else:
            body += ["(이미지 없음 또는 깨짐)", ""]
        if p.multi_wearer_suspected:
            body += ["> [!warning] 여러 명이 찍힌 사진일 수 있다(C2 휴리스틱).", ""]
        body += ["## 속성", "", attr_links(p), "", "## 일치", "",
                 f"- 일치: {', '.join(p.match.matched) or '-'}", f"- 불일치: {', '.join(p.match.mismatched) or '-'}",
                 f"- 미확인: {', '.join(p.match.unknown) or '-'}", "", "## 나온 곳", ""]
        body += [f"- {link(paths[h[1]], data['trends'][h[1]]['style'].name)} → {link(paths[h[2]], data['items'][h[2]]['item'].name)}"
                 f" · score {h[0]:.3f}" for h in hits if h[1] in paths and h[2] in paths]
        for term, rank, ts in pr.get("ranks", []):
            body.append(f"- 트렌드 Top-K `{term}` #{rank} · trend_score {ts:.3f}")
        sb = p.score_breakdown
        if sb.attributes:
            body += ["", "## 점수 분해", "", "| 속성 | weight | base_weight | level | ratio |", "|---|---:|---:|---|---:|"]
            body += [f"| {k} | {a.weight:.3f} | {a.base_weight:.3f} | {a.level or ''} | {a.ratio:.3f} |" for k, a in sb.attributes.items()]
            body += ["", f"base_score {sb.base_score:.3f} · bonus {', '.join(f'{k} +{v:.3f}' for k, v in sb.bonus.items()) or '없음'}"]
        w.note(paths[pid], frontmatter(fm) + "\n".join(body) + "\n")

    for kind, value in sorted(attrs_needed):
        fm = {"type": "attribute", "generated": True, "kind": kind, "value": value, "tags": ["attribute"]}
        w.note(attr_path(kind, value), frontmatter(fm) + f"# {value}\n\n속성 `{kind}`의 표준 값. 백링크에 이 값을 가진 Item·상품이 모인다.\n")

    # 원본 사진 복사(결과에 쓰인 것만)
    snap_dir = shared_dir() / "images" / "snap"
    for row in sorted(snaps_used):
        src = snap_dir / f"{row}.jpg"
        if src.exists():
            w.copy_asset(src, f"assets/snap/{row}.jpg")
        else:
            w.report["missing_snap"].append(str(row))

    w.managed(BASE_PATH, base_yaml())
    for sid in sorted(data["trends"]):
        w.managed(f"91_Canvas/{Path(paths[sid]).stem}.canvas", canvas_json(sid, data, paths))
    w.save_manifest()
    rep = {k: len(v) for k, v in w.report.items()}
    rep["skipped_paths"] = w.report.get("skipped_human", []) + w.report.get("skipped_user_edited", [])
    rep["notes"] = {"trends": len(data["trends"]), "items": len(data["items"]), "products": len(data["products"]),
                    "attributes": len(attrs_needed), "snaps": len(snaps_used)}
    return {"report": rep, "paths": {k: v for k, v in paths.items()}}


def base_yaml() -> str:
    """공식 문서 키만 쓴 Base(레퍼런스 §2-1). 카드 이미지(`image`)·정렬(score↓)은 UI에서 한 번 지정한다."""
    return """filters:
  and:
    - file.inFolder("40_Products")
    - 'note.type == "product"'
properties:
  score:
    displayName: 점수
views:
  - type: cards
    name: 상품 카드
    groupBy:
      property: note.trend
      direction: ASC
    order:
      - file.name
      - note.trend
      - note.item
      - note.score
      - note.image_scope
  - type: table
    name: 상품 표
    order:
      - file.name
      - note.product_id
      - note.trend
      - note.item
      - note.score
      - note.image_scope
      - note.image_status
      - note.source
"""


def canvas_json(sid: str, data: dict[str, Any], paths: dict[str, str], per_item: int = 4) -> str:
    """JSON Canvas 1.0: 트렌드(text) → Item(file 노트) → 상품(03 file 이미지 · 02 text 외부 이미지). 좌표는 격자."""
    tr = data["trends"][sid]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    item_ids = tr["items"]
    col_w, img_w, img_h = 300, 240, 320
    total_w = max(1, len(item_ids)) * col_w
    nodes.append({"id": f"trend-{short_hash(sid, 8)}", "type": "file", "file": paths[sid], "x": total_w // 2 - 220, "y": 0,
                  "width": 440, "height": 200, "color": "6"})
    for i, iid in enumerate(item_ids):
        x = i * col_w
        inode = f"item-{short_hash(iid, 8)}"
        nodes.append({"id": inode, "type": "file", "file": paths[iid], "x": x, "y": 280, "width": 260, "height": 160,
                      "color": "4"})
        edges.append({"id": f"e-{short_hash(sid + iid, 10)}", "fromNode": nodes[0]["id"], "fromSide": "bottom",
                      "toNode": inode, "toSide": "top"})
        for j, pid in enumerate(data["items"][iid]["products"][:per_item]):
            p = data["products"][pid]["p"]
            image, row = image_value(p)
            pnode = f"prod-{short_hash(iid + pid, 10)}"
            y = 500 + j * (img_h + 120)
            if row is not None:
                nodes.append({"id": pnode, "type": "file", "file": f"assets/snap/{row}.jpg", "x": x, "y": y,
                              "width": img_w, "height": img_h})
            else:
                label = (p.name or pid).replace("[", "(").replace("]", ")")
                text = (f"![{img_w}]({image})\n\n" if image else "(이미지 없음)\n\n") + link(paths[pid], label)
                nodes.append({"id": pnode, "type": "text", "text": text, "x": x, "y": y, "width": img_w, "height": img_h + 80})
            edges.append({"id": f"e-{short_hash(iid + pid, 12)}", "fromNode": inode, "fromSide": "bottom", "toNode": pnode,
                          "toSide": "top"})
    return json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, indent=1) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", help="결과 JSON(TrendAnalysisResult v1.1)")
    ap.add_argument("--vault", default=os.environ.get("VAULT"), required=os.environ.get("VAULT") is None)
    ap.add_argument("--products-dir", help="v1.1 이미지 필드 보강용 products v2 폴더(결과에 값이 없을 때만)")
    ap.add_argument("--notes-map", help="id → 볼트 경로 JSON을 쓸 곳(HTML 갤러리의 Obsidian 링크용)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    loaded = []
    for r in a.results:
        res, sha = load_result(Path(r))
        enrich_from_products(res, Path(a.products_dir) if a.products_dir else None)
        loaded.append((res, sha))
    out = export(loaded, Path(a.vault), a.dry_run)
    if a.notes_map:
        Path(a.notes_map).write_text(json.dumps(out["paths"], ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(out["report"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
