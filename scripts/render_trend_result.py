"""결과 JSON(계약 v1.1 `TrendAnalysisResult`) → HTML 갤러리 (T2 작업 5 · 설계 메모 §3.6).

실행:  python scripts/render_trend_result.py RESULT.json [--out DIR] [--products-dir "$SHARED/data/processed/v2"]
출력:  <out>/<request_id>.html (기본 out = $SHARED/results)

구성: 해석 결과(용어별 Tier) → 트렌드마다 Style 인용(근거 + 기사 링크) → Item 카드 하나씩 → 상품 이미지 격자 + 점수 분해
      → 트렌드 단위 Top-K(`resolutions[].top_products`: 순위는 그대로, 표시 순서만 이미지 있는 것 먼저) → 근거 표·한계.
규칙
- Vogue 기사 이미지는 **링크만**(결정 G0-⑤: 임베드·핫링크·다운로드 금지). 상품 이미지만 표시한다.
- 02(catalog·full_set)는 `<img src=URL>`, 03(full_snap)은 C2 원본 `$SHARED/images/snap/<row>.jpg`를 상대 경로로 건다(크롭 없음, Q4).
  같은 사진의 의류는 같은 사진을 가리킨다. `multi_wearer_suspected`면 경고를 단다. `image_status=broken`이면 자리표시.
- `--products-dir`: 결과에 v1.1 이미지 필드가 없을 때(develop v1.0 검색 출력 등) products v2에서 product_id로 보강하고
  보강 건수를 쪽 아래에 적는다. 결과에 값이 있으면 덮어쓰지 않는다.
- 같은 입력이면 같은 출력(시각을 넣지 않고 입력 sha256을 적는다). Obsidian 노트 링크는 `obsidian://open?vault=…&file=…`.
- 이미지 내용 판정(C2 `$SHARED/images/content.parquet`, L0 결정 #203 — **표시층 전용**): product_id로 조인만 한다(구성품은
  parent_id). 표시 순서 = ① 이미지 정상·garment → ② 정상·uncertain/판정 없음 → ③ 정상·non_garment(자리표시 + 원본 링크)
  → ④ 깨짐·없음. 같은 묶음 안에서는 원래 순서(점수·rank)를 지키고, 점수·rank·필터는 바꾸지 않는다. codex_label·scores·
  local_path는 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.schemas.trend_analysis_result import ProductMatch, TrendAnalysisResult  # noqa: E402

VAULT_NAME = "_vault"
IMAGE_FIELDS = ("snap_id", "image_ref", "image_scope", "image_status", "parent_id", "multi_wearer_suspected")
_SNAP_ID = re.compile(r"^musinsa_snap:(\d+):([^:]+)(?::(\d+))?$")


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def snap_parts(product_id: str) -> Optional[tuple[int, str, int]]:
    """03 id 규칙 `musinsa_snap:<row>:<slot>[:<k>]` → (row, slot, k)."""
    m = _SNAP_ID.match(product_id)
    return (int(m.group(1)), m.group(2), int(m.group(3) or 0)) if m else None


def shared_dir() -> Path:
    return Path(os.environ.get("SHARED", ROOT.parent.parent / "_shared"))


def load_result(path: Path) -> tuple[TrendAnalysisResult, str]:
    raw = path.read_bytes()
    return TrendAnalysisResult.model_validate_json(raw), sha256_bytes(raw)


def all_products(res: TrendAnalysisResult) -> Iterable[ProductMatch]:
    for t in res.trends:
        for it in t.items:
            yield from it.similar_products
    for r in res.resolutions:
        yield from r.top_products


def enrich_from_products(res: TrendAnalysisResult, products_dir: Optional[Path]) -> int:
    """결과에 이미지 필드가 비어 있는 상품만 products v2 값으로 채운다. 채운 상품 수를 돌려준다."""
    if not products_dir:
        return 0
    need = {p.product_id for p in all_products(res) if p.image_status is None and p.image_ref is None}
    if not need:
        return 0
    found: dict[str, dict[str, Any]] = {}
    for name in ("otpensource_all.jsonl", "musinsa_snap_all.jsonl"):
        f = products_dir / name
        if not f.exists():
            continue
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if not need - found.keys():
                    break
                pid = line[line.find('"id":"') + 6: line.find('"', line.find('"id":"') + 6)] if '"id":"' in line else ""
                if pid in need:
                    found[pid] = json.loads(line)["node"]
    n = 0
    for p in all_products(res):
        node = found.get(p.product_id)
        if node is None or p.image_status is not None or p.image_ref is not None:
            continue
        for k in IMAGE_FIELDS:
            if getattr(p, k) is None and node.get(k) is not None:
                setattr(p, k, node[k])
        st = p.image_status
        p.has_image = (st == "ok") if st is not None else bool(p.image_url or p.image_ref)
        n += 1
    return n


CONTENT_COLUMNS = ["product_id", "image_content"]
NON_GARMENT_NOTE = "옷이 아닌 이미지일 수 있음(라벨·직물 등, vision 판정 근사)"


def load_content(path: Optional[Path]) -> dict[str, str]:
    """C2 이미지 내용 sidecar → {product_id: garment|non_garment|uncertain}. 파일이 없으면 빈 dict."""
    if not path or not Path(path).exists():
        return {}
    import pandas as pd

    df = pd.read_parquet(path, columns=CONTENT_COLUMNS)
    return {r.product_id: r.image_content for r in df.itertuples(index=False) if isinstance(r.image_content, str)}


def image_content(p: ProductMatch, content: Optional[dict[str, str]]) -> Optional[str]:
    """구성품은 부모 판정을 쓴다(L0 #203). 03 스냅·판정 없음은 None."""
    if not content:
        return None
    return content.get(p.parent_id or p.product_id)


def display_class(p: ProductMatch, content: Optional[dict[str, str]]) -> int:
    """0 정상·garment · 1 정상·uncertain/None · 2 정상·non_garment · 3 깨짐/이미지 없음."""
    if not p.has_image:
        return 3
    c = image_content(p, content)
    return 0 if c == "garment" else (2 if c == "non_garment" else 1)


def order_for_display(products: list[ProductMatch], content: Optional[dict[str, str]],
                      key: Optional[Any] = None) -> list[tuple[int, ProductMatch]]:
    """[(원래 순번 1부터, 상품)] — 표시 묶음 순으로, 같은 묶음 안에서는 원래 순서(또는 key)."""
    idx = list(enumerate(products, start=1))
    return sorted(idx, key=lambda t: (display_class(t[1], content), key(t[1]) if key else t[0]))


def image_for(p: ProductMatch, out_dir: Path, content: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """표시용 이미지: {kind: url|file|none, src, caption, warn, link}."""
    scope = p.image_scope or ("full_snap" if p.source == "musinsa_snap" else "catalog")
    warn = []
    if p.multi_wearer_suspected:
        warn.append("여러 명이 찍힌 사진일 수 있음")
    if p.image_status == "broken":
        return {"kind": "none", "src": "", "caption": "이미지 깨짐(GET 관측)", "warn": warn, "scope": scope}
    if p.source == "musinsa_snap":
        sp = snap_parts(p.product_id)
        row = p.snap_id if p.snap_id is not None else (sp[0] if sp else None)
        if row is None:
            return {"kind": "none", "src": "", "caption": "사진 없음", "warn": warn, "scope": scope}
        src = Path(os.path.relpath(shared_dir() / "images" / "snap" / f"{row}.jpg", out_dir)).as_posix()
        slot = sp[1] if sp else ""
        return {"kind": "file", "src": src, "caption": f"착장 원본 사진 — 이 의류: {slot}", "warn": warn, "scope": scope,
                "row": row}
    url = p.image_url or (p.image_ref if (p.image_ref or "").startswith("http") else None)
    if not url:
        return {"kind": "none", "src": "", "caption": "이미지 URL 없음", "warn": warn, "scope": scope}
    cap = "세트 이미지(구성품이 부모 사진을 함께 씀)" if scope == "full_set" else "상품 컷"
    if image_content(p, content) == "non_garment":  # 상품은 그대로 두고 이미지만 자리표시 + 원본 링크
        return {"kind": "none", "src": "", "caption": NON_GARMENT_NOTE, "warn": warn, "scope": scope, "link": url}
    return {"kind": "url", "src": url, "caption": cap, "warn": warn, "scope": scope}


def obsidian_href(vault_path: str) -> str:
    """obsidian://open?vault=_vault&file=<encodeURIComponent(경로, .md 생략)> — HTML 속성에 넣을 때 &는 &amp;."""
    p = vault_path[:-3] if vault_path.endswith(".md") else vault_path
    return f"obsidian://open?vault={quote(VAULT_NAME, safe='')}&amp;file={quote(p, safe='')}"


def chips(values: Iterable[str], cls: str = "") -> str:
    return "".join(f'<span class="chip {cls}">{esc(v)}</span>' for v in values if v)


def attr_chips(x: Any) -> str:
    approx = set(x.approx or [])
    parts = []
    if x.category_major or x.category_minor:
        parts.append(chips([f"{x.category_major.value if x.category_major else '?'}/{x.category_minor.value if x.category_minor else '-'}"], "cat"))
    for label, vals, kind in (("색", [c.value for c in x.colors], "color"), ("소재", x.materials, "material"),
                              ("패턴", x.patterns, "pattern")):
        parts.append(chips([f"{label} {v}{' ≈' if f'{kind}:{v}' in approx else ''}" for v in vals], kind))
    parts.append(chips([f"{d.type.value if hasattr(d.type, 'value') else d.type}:{d.name}" for d in x.details], "detail"))
    for label, v in (("핏", x.fit), ("세이프", x.shape), ("기장", x.length_body), ("소매", x.length_sleeve)):
        if v is not None:
            parts.append(chips([f"{label} {v.value if hasattr(v, 'value') else v}"], "scalar"))
    return "".join(parts)


def score_table(p: ProductMatch) -> str:
    sb = p.score_breakdown
    rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{a.weight:.3f}</td><td>{a.base_weight:.3f}</td><td>{esc(a.level or '')}</td><td>{a.ratio:.3f}</td></tr>"
        for k, a in sb.attributes.items())
    bonus = ", ".join(f"{esc(k)} +{v:.3f}" for k, v in sb.bonus.items()) or "없음"
    extra = f" · 미확인 core 가중치 {sb.unknown_core_weight:.3f}" if sb.unknown_core_weight else ""
    vw = " · 값 가중" if sb.value_weighted else ""
    return (f'<details><summary>점수 {p.score:.3f} 분해</summary><table class="score"><tr><th>속성</th><th>weight</th>'
            f'<th>base_weight</th><th>level</th><th>ratio</th></tr>{rows}</table>'
            f'<p>base_score {sb.base_score:.3f} · bonus {bonus}{extra}{vw}</p></details>')


def product_card(p: ProductMatch, out_dir: Path, note_paths: dict[str, str], extra_head: str = "",
                 content: Optional[dict[str, str]] = None, order_no: Optional[int] = None) -> str:
    im = image_for(p, out_dir, content)
    if order_no is not None and not extra_head:
        extra_head = f'<div class="rank">점수 순위 {order_no}</div>'
    if im["kind"] == "none":
        orig = (f'<br><a href="{esc(im["link"])}" target="_blank" rel="noopener noreferrer">원본 이미지 보기</a>'
                if im.get("link") else "")
        media = f'<div class="noimg">{esc(im["caption"])}{orig}</div>'
    else:
        media = f'<img loading="lazy" referrerpolicy="no-referrer" src="{esc(im["src"])}" alt="{esc(p.name or p.product_id)}">'
    warn = "".join(f'<p class="warn">⚠ {esc(w)}</p>' for w in im["warn"])
    m = p.match
    match = (chips([f"✓ {v}" for v in m.matched], "ok") + chips([f"✗ {v}" for v in m.mismatched], "bad")
             + chips([f"? {v}" for v in m.unknown], "unk"))
    note = note_paths.get(p.product_id)
    link = f' · <a href="{obsidian_href(note)}">Obsidian</a>' if note else ""
    flags = []
    if p.parent_id:
        flags.append(f"세트 구성품(원 상품 {p.parent_id})")
    if p.kind == "snap_garment":
        flags.append("착장 사진 속 의류(구매 상품 아님)")
    return (f'<figure class="card">{extra_head}{media}<figcaption><b>{esc(p.name or "(이름 없음)")}</b>'
            f'<br><code>{esc(p.product_id)}</code><br><span class="cap">{esc(im["caption"])}</span>{warn}'
            f'<p class="flags">{esc(" · ".join(flags))}</p><p>{match}</p>{attr_chips(p)}{score_table(p)}'
            f'<p class="meta">{esc(p.source)} · {esc(im["scope"])} · status {esc(p.image_status or "-")}{link}</p>'
            f'</figcaption></figure>')


def trend_notice(t: Any, res: TrendAnalysisResult) -> str:
    """판정 자격 안내(T3 표시 규칙): Tier4 LLM 제안은 사람 확인 필요, 기사 근거가 없으면 그렇게 적는다."""
    parts = []
    if t.qualification == "agent_proposal":
        parts.append("LLM 제안(Tier4) — 사람 확인 필요")
    elif t.qualification == "vocabulary_rule":
        parts.append("별칭 사전 규칙(Tier2) — 기사 인용 없음")
    llm = next((r.llm for r in res.resolutions if t.style.id in r.style_ids and r.llm), None)
    if llm is not None and llm.evidence == "none":
        parts.append("기사 근거 없음 — LLM 일반 지식")
    if t.style.season_status in ("mismatch", "unknown"):
        parts.append(f"시즌 {t.style.season_status}(season_policy=report라 결과에 남김)")
    return " · ".join(parts)


def render(res: TrendAnalysisResult, src_sha: str, out_dir: Path, enriched: int = 0,
           note_paths: Optional[dict[str, str]] = None, content: Optional[dict[str, str]] = None) -> str:
    note_paths = note_paths or {}
    content = content or {}
    ev = {e.evidence_id: e for e in res.evidence}

    def ev_link(eid: str) -> str:
        e = ev.get(eid)
        if not e:
            return f"<code>{esc(eid)}</code>"
        title = esc(e.title or e.article_id)
        href = f' href="{esc(e.url)}" target="_blank" rel="noopener"' if e.url else ""
        return f'<a{href}>{title}</a> <span class="pub">{esc((e.published_at or "")[:10])}</span>'

    def quote_block(eids: list[str]) -> str:
        out = []
        for eid in eids:
            e = ev.get(eid)
            if e:
                out.append(f"<blockquote>“{esc(e.quote)}” — {ev_link(eid)}</blockquote>")
        return "".join(out)

    h: list[str] = []
    af = res.applied_filters
    h.append(f"<h1>트렌드 분석 결과 <code>{esc(res.request_id)}</code></h1>")
    h.append(f'<p class="meta">status <b>{esc(res.status)}</b> · schema {esc(res.schema_version)} · 입력 sha256 {src_sha[:12]}'
             + (f" · 시즌 {esc(af.season.model_dump_json(exclude_none=True))}" if af else "") + "</p>")
    if res.applied_settings:
        s = res.applied_settings
        h.append(f'<p class="meta">설정: weights {esc(s.weights_sha)} · season_policy {esc(s.season_policy)} · top_k {s.top_k}'
                 f' · 이미지 우선 동점 {esc(s.image_first_tiebreak)} · γ {s.core_unknown_decay}</p>')
    if res.resolutions:
        rows = "".join(
            f"<tr><td>{esc(r.term)}</td><td>{esc(r.status)}</td><td>{esc(r.tier or '-')}</td><td>{esc(r.match_mode or '-')}</td>"
            f"<td>{esc(r.qualification or '-')}</td><td>{'' if r.similarity is None else f'{r.similarity:.3f}'}</td>"
            f"<td>{esc(' → '.join(f'T{s.tier}:{s.outcome}:{s.code}' for s in r.steps))}</td><td>{len(r.top_products)}</td></tr>"
            for r in res.resolutions)
        h.append('<h2>용어 해석</h2><table class="res"><tr><th>용어</th><th>상태</th><th>Tier</th><th>판정 방식</th><th>근거 자격</th>'
                 f'<th>유사도</th><th>단계</th><th>Top-K</th></tr>{rows}</table>')
    for t in res.trends:
        st = t.style
        badges = chips([x for x in (t.match_mode, t.qualification, st.season_status and f"시즌 {st.season_status}",
                                    "가상 트렌드" if st.virtual else None, t.term and f"용어 {t.term}") if x], "badge")
        h.append(f'<section class="trend" id="{esc(st.id)}"><h2>{esc(st.name)} {badges}</h2>')
        h.append(f'<p class="meta">시즌 {esc(", ".join(st.seasons) or "미상")} · 별칭 {esc(", ".join(st.aliases) or "-")}'
                 f' · id <code>{esc(st.id)}</code>' + (f' · <a href="{obsidian_href(note_paths[st.id])}">Obsidian</a>'
                                                     if st.id in note_paths else "") + "</p>")
        notice = trend_notice(t, res)
        if notice:
            h.append(f'<p class="warn">{esc(notice)}</p>')
        h.append(f"<p>{esc(st.definition)}</p>")
        for d in st.description:
            h.append(f"<p>{esc(d.text)}</p>{quote_block(d.evidence_ids)}")
        if st.focus_core or st.focus_supporting:
            h.append(f'<p>핵심 속성 {chips(st.focus_core, "core")} 보조 {chips(st.focus_supporting, "sup")}</p>')
        for it in t.items:
            vlinks = " ".join(f'<a href="{esc(u)}" target="_blank" rel="noopener">기사 이미지 {i + 1}</a>'
                              for i, u in enumerate(it.image_urls))
            h.append(f'<div class="item"><h3>{esc(it.name)}</h3><p>{attr_chips(it)}</p>'
                     + (f'<p class="vogue">Vogue 이미지(링크만): {vlinks}</p>' if vlinks else "")
                     + (f"<p>스타일링: {esc(it.styling_raw)}</p>" if it.styling_raw else "")
                     + quote_block(it.evidence_ids))
            if it.similar_products:
                cards = "".join(product_card(p, out_dir, note_paths, content=content, order_no=i)
                                for i, p in order_for_display(it.similar_products, content))
                h.append(f'<div class="grid">{cards}</div>')
            else:
                h.append('<p class="meta">비슷한 상품 없음</p>')
            h.append("</div>")
        picks = [p for r in res.resolutions if st.id in r.style_ids for p in r.top_products]
        if picks:
            ordered = order_for_display(picks, content, key=lambda p: p.rank)  # rank는 그대로, 표시 묶음만 앞뒤
            cards = "".join(product_card(p, out_dir, note_paths, f'<div class="rank">#{p.rank} · {esc(p.item_name)} · '
                                                                 f'trend_score {p.trend_score:.3f}</div>', content)
                            for _, p in ordered)
            h.append(f'<h3>트렌드 Top-K ({len(picks)})</h3><div class="grid">{cards}</div>')
        h.append("</section>")
    if res.evidence:
        rows = "".join(f"<tr><td><code>{esc(e.evidence_id)}</code></td><td>{ev_link(e.evidence_id)}</td><td>{e.block_idx}</td>"
                       f"<td>{esc(e.quote)}</td></tr>" for e in res.evidence)
        h.append(f'<h2>근거</h2><table class="ev"><tr><th>id</th><th>기사</th><th>블록</th><th>인용</th></tr>{rows}</table>')
    if res.limitations or enriched:
        lim = list(res.limitations)
        if enriched:
            lim.append(f"표시용 보강: 결과에 v1.1 이미지 필드가 없던 상품 {enriched}개를 products v2에서 product_id로 채웠다(T2 렌더러).")
        h.append("<h2>한계·알림</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in lim) + "</ul>")
    h.append('<p class="meta">이미지: 02 = 무신사 CDN URL(점검 시점 관측) · 03 = C2 원본 사진(크롭·재인코딩 없음) · '
             'Vogue 기사 이미지는 링크만 둔다. 카드 표시 순서 = 이미지 정상·옷 → 정상·판정 보류 → 정상·옷 아닐 수 있음 → '
             '이미지 없음(같은 묶음 안은 점수 순). 점수·순위는 바꾸지 않는다'
             + (f' · 이미지 내용 판정 {len(content):,}개(C2 vision 근사)' if content else '') + '.</p>')
    return PAGE.format(title=esc(f"트렌드 결과 {res.request_id}"), body="\n".join(h))


PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:-apple-system,"Apple SD Gothic Neo",sans-serif;margin:24px;color:#222;max-width:1400px}}
h1{{font-size:22px}} h2{{font-size:19px;margin-top:28px}} h3{{font-size:16px}}
.meta{{color:#666;font-size:12px}} code{{font-size:11px;color:#555}}
.trend{{border-top:2px solid #333;padding-top:8px;margin-top:24px}}
.item{{border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:14px 0;background:#fafafa}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}}
.card{{margin:0;border:1px solid #ddd;border-radius:6px;background:#fff;overflow:hidden;font-size:12px}}
.card img{{width:100%;aspect-ratio:3/4;object-fit:contain;background:#f0f0f0;display:block}}
.noimg{{width:100%;aspect-ratio:3/4;background:#eee;color:#999;display:flex;align-items:center;justify-content:center}}
figcaption{{padding:6px 8px}} .cap{{color:#555}} .warn{{color:#b45309;margin:2px 0}} .flags{{color:#555;margin:2px 0}}
.chip{{display:inline-block;border-radius:10px;padding:1px 7px;margin:1px;background:#eef;font-size:11px}}
.chip.ok{{background:#dcfce7}} .chip.bad{{background:#fee2e2}} .chip.unk{{background:#f3f4f6;color:#666}}
.chip.badge{{background:#fde68a;font-size:11px;font-weight:normal}} .chip.cat{{background:#e0e7ff}}
.rank{{background:#111;color:#fff;font-size:11px;padding:3px 6px}}
blockquote{{margin:6px 0 6px 10px;padding-left:8px;border-left:3px solid #ccc;color:#444;font-size:13px}}
table{{border-collapse:collapse;font-size:12px}} td,th{{border:1px solid #ddd;padding:3px 6px;vertical-align:top}}
.pub{{color:#888;font-size:11px}}
</style></head><body>
{body}
</body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", help="결과 JSON (TrendAnalysisResult v1.1)")
    ap.add_argument("--out", help="출력 폴더(기본 $SHARED/results)")
    ap.add_argument("--products-dir", help="v1.1 이미지 필드 보강용 products v2 폴더")
    ap.add_argument("--notes-map", help="export_obsidian.py가 쓴 {id: 볼트 경로} JSON(노트 링크용)")
    ap.add_argument("--content", default=str(shared_dir() / "images" / "content.parquet"),
                    help="C2 이미지 내용 sidecar(표시 순서용, 없으면 무시). 끄려면 --content ''")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    from trend_agent.ingestion.normalization.output import resolve_out, write_text_atomic

    out = resolve_out(a.out or shared_dir() / "results")
    res, sha = load_result(Path(a.result))
    n = enrich_from_products(res, Path(a.products_dir) if a.products_dir else None)
    notes = json.loads(Path(a.notes_map).read_text(encoding="utf-8")) if a.notes_map else {}
    path = out / f"{res.request_id}.html"
    content = load_content(Path(a.content)) if a.content else {}
    write_text_atomic(path, render(res, sha, out, n, notes, content))
    print(json.dumps({"html": str(path), "trends": len(res.trends), "enriched": n,
                      "products": sum(1 for _ in all_products(res))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
