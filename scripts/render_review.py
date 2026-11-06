"""정답(label) JSON을 검수하기 쉬운 문서(md)로 바꾼다. 원본 링크가 맨 위에 들어간다.

실행:  python scripts/render_review.py [article_id ...]
출력:  eval/datasets/article_extraction/review/<article_id>.md   (로컬 전용, gitignore)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "eval" / "datasets" / "article_extraction"
LABELS, REVIEW = BASE / "labels", BASE / "review"
RAW = ROOT / "data" / "raw" / "vogue_kr"


def load_articles() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for p in RAW.glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            rows.setdefault(r["article_id"], r)
    return rows


def cell(v) -> str:
    if v is None or v == []:
        return "—"
    return str(v).replace("|", "\\|").replace("\n", " ")


def arrow(raw, val) -> str:
    """원문→표준 값. 표준 값이 없으면 '(표준 없음)'."""
    if val is None:
        return f"{raw}→(표준 없음)"
    return f"{raw}→{val}" if raw != val else f"{raw}"


def pairs(items, raw="raw", val="value") -> str:
    out = [arrow(x.get(raw), x.get(val)) for x in items]
    return ", ".join(out) if out else "—"


def render(doc: dict, art: dict) -> str:
    meta, ext = doc["label_meta"], doc["extraction"]
    L = [f"# 검수: {meta['title']}", ""]
    L += [f"- **원본 링크**: {meta['url']}", f"- article_id `{meta['article_id']}` · 발행 {meta.get('published', '')} · 상태 **{meta['status']}** · 라벨러 {meta['labeler']} · 검토자 {meta['reviewer'] or '(미검수)'} · 기준 v{meta['guideline_version']}", ""]
    if meta.get("review_questions"):
        L += ["## 검수자에게 묻는 것", ""] + [f"{n}. {q}" for n, q in enumerate(meta["review_questions"], 1)] + [""]
    L += ["## 검수 방법", "", "각 줄에서 **틀린 부분**만 알려 주세요 (예: `Item 3: 색 → 블랙`, `Item 7 삭제`, `Style 시즌 추가`). 맞으면 그대로 두세요. 근거 `[번호]`는 기사 문단 번호입니다.", ""]
    for s in ext["styles"]:
        L += [f"## Style: {s['name']} ({s['name_origin']})", ""]
        L += [f"- **시즌**: {', '.join(f'{x['year']}{x['season']}' for x in s['seasons']) or '(없음)'}", f"- **정의**: {s['definition']}"]
        for d in s["description"]:
            L.append(f"- **기획 참고**: {d['text']}  ({'; '.join('[%d] “%s”' % (e['block_idx'], e['quote']) for e in d['evidence'])})")
        for f in s["focus_attributes"]:
            L.append(f"- **핵심 속성**: {f['attribute']} = {f['level']}  ({'; '.join('[%d] “%s”' % (e['block_idx'], e['quote']) for e in f['evidence'])})")
        L.append(f"- **Style 근거**: " + "; ".join("[%d] “%s”" % (e["block_idx"], e["quote"]) for e in s["evidence"]))
        L += ["", f"### Item {len(s['items'])}개", "", "| # | 이름 | 상분류 | 하분류 (원문) | 색 | 소재 | 패턴 | 디테일 | 핏 | 세이프 | 기장 | 착용·조합 | 근거 |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for n, it in enumerate(s["items"], 1):
            cat = f"{cell(it.get('category_minor'))} ({cell(it.get('category_raw'))})"
            det = ", ".join(f"{d['raw']}→{d.get('value')}[{d['type']}]" + (f" ({d['motif']}/{d['placement']})" if d.get('motif') or d.get('placement') else "") for d in it["details"]) or "—"
            fit = arrow(it["fit_raw"], it.get("fit")) if it.get("fit_raw") else cell(it.get("fit"))
            shape = arrow(it["shape_raw"], it.get("shape")) if it.get("shape_raw") else "—"
            ln = ", ".join(x for x in [f"기장 {arrow(it['length_body_raw'], it.get('length_body'))}" if it.get("length_body_raw") else "", f"소매 {arrow(it['length_sleeve_raw'], it.get('length_sleeve'))}" if it.get("length_sleeve_raw") else ""] if x) or "—"
            sty = cell(it.get("styling_raw")) + (f" / {it['styling_accessory']}" if it.get("styling_accessory") else "")
            ev = "; ".join("[%d]" % e["block_idx"] for e in it["evidence"])
            L.append(f"| {n} | {cell(it['name'])} | {cell(it.get('category_major'))} | {cat} | {pairs(it['colors'])} | {pairs(it['materials'])} | {pairs(it['patterns'])} | {det} | {fit} | {shape} | {ln} | {sty} | {ev} |")
        L += ["", "**Item 근거 문장**", ""]
        for n, it in enumerate(s["items"], 1):
            L.append(f"{n}. {it['name']}: " + " / ".join("[%d] “%s”" % (e["block_idx"], e["quote"]) for e in it["evidence"]))
        L.append("")
    if meta.get("notes"):
        L += ["## 라벨러 메모", ""] + [f"- {n}" for n in meta["notes"]] + [""]
    L += ["## 기사 문단 (번호 확인용)", ""]
    for b in art["blocks"]:
        if b["type"] == "paragraph":
            L.append(f"[{b['idx']}] {b['text']}")
        elif b["type"] == "heading":
            L.append(f"\n**[{b['idx']}] {b['text']}**")
    return "\n".join(L) + "\n"


def main() -> None:
    REVIEW.mkdir(parents=True, exist_ok=True)
    arts = load_articles()
    ids = sys.argv[1:] or sorted(p.stem for p in LABELS.glob("*.json"))
    for i in ids:
        doc = json.loads((LABELS / f"{i}.json").read_text(encoding="utf-8"))
        (REVIEW / f"{i}.md").write_text(render(doc, arts[i]), encoding="utf-8")
        print("wrote", REVIEW / f"{i}.md")


if __name__ == "__main__":
    main()
