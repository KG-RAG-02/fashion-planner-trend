"""정답(label) JSON을 검수하기 쉬운 문서(md)로 바꾼다. 원본 링크가 맨 위에 들어간다.

실행:  python scripts/render_review.py [article_id ...]
출력:  data/labels/review/<article_id>.md   (로컬 전용, gitignore)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "data" / "labels"
REVIEW = LABELS / "review"
RAW = ROOT / "data" / "raw"


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


APPROVAL_GUIDE = [
    "## 사용자 승인 방법",
    "",
    "- gold 전환은 **사용자만** 합니다. `<article_id> gold 승인` 또는 `<article_id> 수정: …`처럼 말해 주세요.",
    "- 승인하면 라벨 JSON에 `status: gold`·`reviewer`·`reviewed_at`을 적고 지시 원문을 `notes`에 남긴 뒤 매니페스트 `label_status`를 맞춥니다.",
    "- JSON이 원본이고 이 문서는 보기용입니다.",
    "",
]


def render_t4_review(meta: dict) -> list[str]:
    """label_meta.t4_review = [{target, opinion, basis}] → '1차 검수 의견' 표 + 승인 방법. (키 이름 `t4_review`는 이미 만들어진 라벨 파일에 있는 이름이라 그대로 읽는다.)"""
    rows = meta.get("t4_review") or []
    if not rows:
        return []
    L = ["## 1차 검수 의견", "", "| 대상 | 의견 | 근거 |", "|---|---|---|"]
    L += [f"| {cell(r.get('target'))} | {cell(r.get('opinion'))} | {cell(r.get('basis'))} |" for r in rows]
    return L + [""] + APPROVAL_GUIDE


def render(doc: dict, art: dict) -> str:
    meta, ext = doc["label_meta"], doc["extraction"]
    L = [f"# 검수: {meta['title']}", ""]
    L += [f"- **원본 링크**: {meta['url']}", f"- article_id `{meta['article_id']}` · 발행 {meta.get('published', '')} · 상태 **{meta['status']}** · 라벨러 {meta['labeler']} · 검토자 {meta['reviewer'] or '(미검수)'} · 기준 v{meta['guideline_version']}", ""]
    L += render_t4_review(meta)
    if not ext["styles"]:
        L += ["## 정답: Style 없음 (`styles = []`, 음성 대조군)", "", "제외 근거는 아래 라벨러 메모에 있습니다. 기사에 트렌드가 있다고 보시면 알려 주세요.", ""]
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
