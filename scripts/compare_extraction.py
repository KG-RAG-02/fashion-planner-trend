"""기사 추출 비교: 정답 라벨(labels 링크 + manifest split) vs 예측(백엔드별 추출 디렉터리). 결과는 **건수**로만 보고한다.

문서: eval/README.md(건수 보고 규칙), eval/guidelines/article_extraction.md, docs/schemas/article_extraction.md §5(V2·V3).

실행 (레포 루트):
  python scripts/compare_extraction.py --pred openai_api="data/processed/extractions/<pv>" [--pred 라벨2="…"] \
      [--split dev|test|all] [--ids 857922,…] [--include-review] [--no-length-rule] [--out eval/results/extraction/<run>] [--confirm-test]

- 예측 파일: <dir>/<aid>.json(검증 통과) · <dir>/_review/<aid>.json(검증 실패). 형식은 {"label_meta","extraction"} 또는 ArticleExtraction 단독.
  `_review`는 기본으로 "예측 없음(검토 대기)"으로 세고, --include-review면 함께 비교한다.
- test 기사는 --confirm-test가 있어야 열린다. 열 때마다 eval/results/test_access.jsonl에 기록한다.
- 기장: '롱'은 롱으로 비교한다(D-38, 2026-11-10 L-맥시 폐기). --length-rule은 L-맥시(하의·원피스 롱 → 맥시)를 켜던 이전 보고서 재현용이다. --no-length-rule은 이제 기본과 같다(받기만 함).
- 목록 속성(색·소재·패턴·디테일): 정답 표준 값을 예측이 표준 값 없이 같은 뜻의 원문(raw)으로만 냈으면 "원문 일치"로 센다(누락 아님, D-43).
출력: <out>/report.md(행 = 지표, 열 = 백엔드) · details.jsonl(기사별 차이) · summary.json(건수·실행 메타).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from trend_agent.schemas.article_extraction import ArticleExtraction  # noqa: E402
from trend_agent.schemas.graph_schema import normalize_term  # noqa: E402

# 평가 비교용 느슨한 이름 키: 그래프의 normalize_term(결정 D-11, 엄격)에 더해 끝의 일반어를 1회 지운다.
# 정답 라벨과 추출 결과의 Style 이름을 맞출 때만 쓰며, 그래프의 이름 일치 판정에는 쓰지 않는다.
_LOOSE_SUFFIXES = ("fashion", "style", "trend", "look", "스타일", "트렌드", "패션", "코디", "룩")


def _norm(text: str) -> str:
    s = normalize_term(text)
    for suffix in _LOOSE_SUFFIXES:
        if s.endswith(suffix) and len(s) - len(suffix) >= 2:
            return s[: -len(suffix)]
    return s
from trend_agent.schemas.vocabulary import BodyLength  # noqa: E402
import validate_labels as VL  # noqa: E402

LABELS = ROOT / "eval" / "datasets" / "article_extraction" / "labels"
MANIFEST = ROOT / "eval" / "datasets" / "article_extraction" / "manifest.yaml"
SCALARS = ("category_major", "category_minor", "fit", "shape", "length_body", "length_sleeve", "gender")
LISTS = ("colors", "materials", "patterns", "details")
STRICT_SIM, RELAXED_SIM, STYLE_DICE = 0.5, 0.6, 0.6
# report.md 행 순서 (그룹 → 칸 이름)
GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("기사", ("대상", "비교", "스키마 실패", "예측 없음(검토 대기)", "예측 없음(파일 없음)", "검토 대기 포함 비교")),
    ("음성 기사", ("기사 수", "Style 낸 기사", "낸 Style 수")),
    ("Style", ("일치", "유사 이름", "누락", "오추출")),
    ("Style 별칭", ("일치", "누락", "오추출")),
    ("시즌", ("완전", "부분", "불일치", "둘 다 없음")),
    ("focus (속성, level)", ("일치", "level 다름", "누락", "오추출")),
    ("Item", ("strict 일치", "하분류 불일치 매칭", "누락(범위 내)", "누락(범위 밖)", "오추출(범위 내)", "오추출(범위 밖)")),
    *[(f"속성 {f}", ("일치", "불일치", "누락", "오추출", "둘 다 null")) for f in SCALARS],
    *[(f"속성 {f}", ("일치", "원문 일치", "누락", "오추출", "표준값 없음(정답)", "표준값 없음(예측)")) for f in LISTS],
    ("예측 근거", ("V2 위반", "V3 위반")),
    ("평가 쪽 보정", ("기장 롱→맥시",)),
]


# ---------- 정규화·유사도 ----------
_PAREN = re.compile(r"[(（]([^)）]*)[)）]")
_SEP = re.compile(r"[,·/]")


def style_keys(name: str, aliases=()) -> set[str]:
    """Style 비교 키 = norm(괄호 밖) ∪ norm(괄호 안 항목, ,·/ 분리) ∪ norm(aliases). norm = 계약 v1.1 normalize_term."""
    parts = [_PAREN.sub(" ", name)] + [p for inner in _PAREN.findall(name) for p in _SEP.split(inner)] + list(aliases)
    return {k for k in (_norm(p) for p in parts) if k}


def _bigrams(s: str) -> Counter:
    s = VL.squash(s or "").lower()
    if len(s) < 2:
        return Counter([s]) if s else Counter()
    return Counter(s[i:i + 2] for i in range(len(s) - 1))


def dice(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    n = sum(A.values()) + sum(B.values())
    return 2 * sum((A & B).values()) / n if n else 0.0


def _v(x):
    return getattr(x, "value", x)


def text_sim(g, p) -> float:
    a, b = VL.squash(g.raw_text), VL.squash(p.raw_text)
    contain = 1.0 if a and b and (a in b or b in a) else 0.0
    return max(contain, dice(g.raw_text, p.raw_text), dice(g.name, p.name))


def block_overlap(g, p) -> int:
    return len({e.block_idx for e in g.evidence} & {e.block_idx for e in p.evidence})


def same_minor(g, p) -> bool:
    if g.category_minor is None and p.category_minor is None:
        return g.category_major == p.category_major
    return g.category_minor == p.category_minor


def _greedy(cands, ug: set, up: set, kind: str, pairs: list) -> None:
    for c in sorted(cands):
        i, j = c[-2], c[-1]
        if i in ug and j in up:
            pairs.append((i, j, kind))
            ug.discard(i)
            up.discard(j)


def match_styles(gold, pred):
    """1:1 탐욕 매칭. 키 교집합(크기, 이름 Dice 순) → 남은 것 중 이름 Dice ≥ 0.6 = 유사 이름. 동점은 정답 순서 → 예측 순서."""
    gk = [style_keys(s.name, s.aliases) for s in gold]
    pk = [style_keys(s.name, s.aliases) for s in pred]
    ug, up, pairs = set(range(len(gold))), set(range(len(pred))), []
    _greedy([(-len(gk[i] & pk[j]), -dice(_norm(gold[i].name), _norm(pred[j].name)), i, j)
             for i in ug for j in up if gk[i] & pk[j]], ug, up, "exact", pairs)
    sim = [(-d, i, j) for i in ug for j in up if (d := dice(_norm(gold[i].name), _norm(pred[j].name))) >= STYLE_DICE]
    _greedy(sim, ug, up, "similar", pairs)
    return pairs, sorted(ug), sorted(up)


def match_items(gold_items, pred_items):
    """strict = 하분류 같음(둘 다 null이면 상분류 같음) ∧ 원문 유사도 ≥ 0.5 / relaxed = 하분류 무시, 유사도 ≥ 0.6. 동점은 근거 블록 겹침."""
    ug, up, pairs = set(range(len(gold_items))), set(range(len(pred_items))), []
    sims = {(i, j): text_sim(gold_items[i], pred_items[j]) for i in ug for j in up}
    ov = {(i, j): block_overlap(gold_items[i], pred_items[j]) for i in ug for j in up}
    _greedy([(-sims[i, j], -ov[i, j], i, j) for (i, j) in sims if sims[i, j] >= STRICT_SIM and same_minor(gold_items[i], pred_items[j])],
            ug, up, "strict", pairs)
    _greedy([(-sims[i, j], -ov[i, j], i, j) for (i, j) in sims if i in ug and j in up and sims[i, j] >= RELAXED_SIM], ug, up, "relaxed", pairs)
    return pairs, sorted(ug), sorted(up)


# ---------- 집계 ----------
def _season_verdict(g, p) -> str:
    G, P = {s.key for s in g.seasons}, {s.key for s in p.seasons}
    if not G and not P:
        return "둘 다 없음"
    if G == P:
        return "완전"
    return "부분" if G & P else "불일치"


def _list_values(it, field):
    if field == "details":
        vals = [(_v(d.type), d.value) for d in it.details]
        return {v for v in vals if v[1] is not None}, sum(1 for v in vals if v[1] is None)
    vals = [_v(x.value) for x in getattr(it, field)]
    return {v for v in vals if v is not None}, sum(1 for v in vals if v is None)


def _entries(it, field):
    """목록 속성의 (표준 값 키, raw) 쌍. 표준 값 키는 _list_values와 같은 모양(details는 (type, value))."""
    if field == "details":
        return [((_v(d.type), d.value) if d.value is not None else None, d.raw or "") for d in it.details]
    return [(_v(x.value), x.raw or "") for x in getattr(it, field)]


def _value_name(key) -> str:
    return str(key[1] if isinstance(key, tuple) else key)


def raw_equivalent(gold_raw: str, gold_value: str, pred_raw: str) -> bool:
    """D-43 원문 일치: 정답 raw ↔ 예측 raw가 (공백·기호 무시) 포함 관계이거나 2-gram Dice ≥ 0.5, 또는 예측 raw가 정답 표준 값 이름을 포함."""
    a, b, v = VL.squash(gold_raw or "").lower(), VL.squash(pred_raw or "").lower(), VL.squash(gold_value or "").lower()
    if not b:
        return False
    if a and (a in b or b in a or dice(gold_raw, pred_raw) >= 0.5):
        return True
    return bool(v) and v in b


def raw_matches(g, p, field) -> list[tuple[str, str]]:
    """정답에는 표준 값이 있는데 예측에는 없고, 예측이 표준 값 없이(value=null) 같은 뜻의 원문만 낸 경우 → [(정답 값, 예측 raw)].
    예측 raw 하나는 정답 값 하나에만 쓴다."""
    G = {k for k, _ in _entries(g, field) if k is not None}
    P = {k for k, _ in _entries(p, field) if k is not None}
    pred_raws = [r for k, r in _entries(p, field) if k is None and r]
    out: list[tuple[str, str]] = []
    for key, graw in _entries(g, field):
        if key is None or key not in G - P:
            continue
        hit = next((r for r in pred_raws if raw_equivalent(graw, _value_name(key), r)), None)
        if hit is not None:
            pred_raws.remove(hit)
            G.discard(key)
            out.append((_value_name(key), hit))
    return out


def compare_item_attrs(g, p, c: Counter) -> dict:
    out = {}
    for f in SCALARS:
        gv, pv = _v(getattr(g, f)), _v(getattr(p, f))
        verdict = ("둘 다 null" if gv is None and pv is None else "누락" if pv is None else "오추출" if gv is None
                   else "일치" if gv == pv else "불일치")
        c[f"속성 {f}|{verdict}"] += 1
        if verdict != "일치" and verdict != "둘 다 null":
            out[f] = {"gold": gv, "pred": pv, "verdict": verdict}
    for f in LISTS:
        (G, gn), (P, pn) = _list_values(g, f), _list_values(p, f)
        rm = raw_matches(g, p, f)  # D-43: 표준 값 없이 원문만 맞힌 것은 누락이 아니라 "원문 일치"
        matched_names = {v for v, _ in rm}
        missed = {k for k in G - P if _value_name(k) not in matched_names}
        c[f"속성 {f}|일치"] += len(G & P)
        c[f"속성 {f}|원문 일치"] += len(rm)
        c[f"속성 {f}|누락"] += len(missed)
        c[f"속성 {f}|오추출"] += len(P - G)
        c[f"속성 {f}|표준값 없음(정답)"] += gn
        c[f"속성 {f}|표준값 없음(예측)"] += pn - len(rm)
        if G != P:
            out[f] = {"누락": sorted(map(str, missed)), "오추출": sorted(map(str, P - G))}
            if rm:
                out[f]["원문 일치"] = [f"{v} ← {r}" for v, r in rm]
    return out


def apply_length_rule(ext: ArticleExtraction) -> int:
    """폐기된 L-맥시(D-38) 재현용: 하의·원피스의 length_body '롱' → '맥시'. 바꾼 건수. 기본으로는 쓰지 않는다."""
    n = 0
    for s in ext.styles:
        for it in s.items:
            if _v(it.category_major) in ("하의", "원피스") and _v(it.length_body) == "롱":
                it.length_body = BodyLength("맥시")
                n += 1
    return n


def _scope(it) -> str:
    return "범위 내" if it.category_major is not None else "범위 밖"


def compare_article(gold: ArticleExtraction, pred: ArticleExtraction, c: Counter, text_by_idx: dict | None) -> dict:
    """한 기사 비교 → Counter c에 건수를 더하고 기사별 차이(details)를 돌려준다."""
    det = {"styles": [], "missed_styles": [], "false_styles": []}
    if not gold.styles:
        c["음성 기사|기사 수"] += 1
        if pred.styles:
            c["음성 기사|Style 낸 기사"] += 1
            c["음성 기사|낸 Style 수"] += len(pred.styles)
    pairs, mg, mp = match_styles(gold.styles, pred.styles)
    for i, j, kind in pairs:
        g, p = gold.styles[i], pred.styles[j]
        c["Style|" + ("일치" if kind == "exact" else "유사 이름")] += 1
        ga, pa = {_norm(a) for a in g.aliases}, {_norm(a) for a in p.aliases}
        c["Style 별칭|일치"] += len(ga & pa)
        c["Style 별칭|누락"] += len(ga - pa)
        c["Style 별칭|오추출"] += len(pa - ga)
        sv = _season_verdict(g, p)
        c[f"시즌|{sv}"] += 1
        gf, pf = {_v(f.attribute): _v(f.level) for f in g.focus_attributes}, {_v(f.attribute): _v(f.level) for f in p.focus_attributes}
        for a in gf.keys() | pf.keys():
            c["focus (속성, level)|" + ("누락" if a not in pf else "오추출" if a not in gf else "일치" if gf[a] == pf[a] else "level 다름")] += 1
        ipairs, ig, ip = match_items(g.items, p.items)
        items = []
        for a, b, k in ipairs:
            c["Item|" + ("strict 일치" if k == "strict" else "하분류 불일치 매칭")] += 1
            items.append({"gold": g.items[a].name, "pred": p.items[b].name, "kind": k, "diff": compare_item_attrs(g.items[a], p.items[b], c)})
        for a in ig:
            c[f"Item|누락({_scope(g.items[a])})"] += 1
        for b in ip:
            c[f"Item|오추출({_scope(p.items[b])})"] += 1
        det["styles"].append({"gold": g.name, "pred": p.name, "kind": kind, "season": sv,
                              "gold_seasons": [s.key for s in g.seasons], "pred_seasons": [s.key for s in p.seasons],
                              "focus": {"gold": gf, "pred": pf}, "items": items,
                              "missed_items": [g.items[a].name for a in ig], "false_items": [p.items[b].name for b in ip]})
    for i in mg:
        c["Style|누락"] += 1
        for it in gold.styles[i].items:
            c[f"Item|누락({_scope(it)})"] += 1
        det["missed_styles"].append(gold.styles[i].name)
    for j in mp:
        c["Style|오추출"] += 1
        for it in pred.styles[j].items:
            c[f"Item|오추출({_scope(it)})"] += 1
        det["false_styles"].append(pred.styles[j].name)
    if text_by_idx is not None:
        v2, v3 = VL.check_quotes(pred, text_by_idx), VL.check_raws(pred, text_by_idx)
        c["예측 근거|V2 위반"] += len(v2)
        c["예측 근거|V3 위반"] += len(v3)
        det["v2"], det["v3"] = v2, v3
    return det


# ---------- 입력 ----------
def load_manifest(path: Path = MANIFEST) -> dict[str, dict]:
    return {a["article_id"]: a for a in yaml.safe_load(path.read_text(encoding="utf-8"))["articles"]}


def load_gold(ids, labels_dir: Path = LABELS) -> dict[str, ArticleExtraction]:
    return {i: ArticleExtraction.model_validate(json.loads((labels_dir / f"{i}.json").read_text(encoding="utf-8"))["extraction"]) for i in ids}


def load_pred(pred_dir: Path, aid: str, include_review: bool):
    """→ (ArticleExtraction | None, status, label_meta). status ∈ ok | review | review_compared | missing | schema_fail"""
    def _find(d: Path) -> Path:  # <aid>.json, 없으면 프롬프트 버전 접두어 파일 <v?>_<aid>.json (실험 실행기)
        p = d / f"{aid}.json"
        if not p.exists():
            hits = sorted(d.glob(f"v*_{aid}.json"))
            if hits:
                return hits[0]
        return p

    f, r = _find(pred_dir), _find(pred_dir / "_review")
    if f.exists():
        src, status = f, "ok"
    elif r.exists():
        if not include_review:
            return None, "review", {}
        src, status = r, "review_compared"
    else:
        return None, "missing", {}
    doc = json.loads(src.read_text(encoding="utf-8"))
    meta = doc.get("label_meta", {}) if isinstance(doc, dict) and "extraction" in doc else {}
    body = doc["extraction"] if isinstance(doc, dict) and "extraction" in doc else doc
    try:
        return ArticleExtraction.model_validate(body), status, meta
    except Exception:  # pydantic ValidationError 등
        return None, "schema_fail", meta


def select_ids(manifest: dict, labels_dir: Path, split: str, ids: list[str] | None) -> list[str]:
    labeled = {p.stem for p in labels_dir.glob("*.json")}
    if ids:
        missing = [i for i in ids if i not in labeled]
        if missing:
            raise SystemExit(f"라벨이 없는 id: {missing}")
        return list(ids)
    want = {"dev": {"dev"}, "test": {"test"}, "all": {"dev", "test", None}}[split]
    return sorted(i for i in labeled if manifest.get(i, {}).get("split") in want)


def needs_test_confirm(manifest: dict, ids: list[str]) -> list[str]:
    return [i for i in ids if manifest.get(i, {}).get("split") == "test"]


def git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def log_test_access(path: Path, preds: dict[str, Path], test_ids: list[str], prompt_versions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "kind": "compare_extraction", "pred": {k: str(v) for k, v in preds.items()}, "test_ids": test_ids,
           "prompt_version": prompt_versions, "git_head": git_head()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- 실행 ----------
def run(preds: dict[str, Path], ids: list[str], gold: dict, include_review: bool, length_rule: bool, texts: dict | None):
    counts: dict[str, Counter] = {}
    details: list[dict] = []
    metas: dict[str, Counter] = {}
    for name, d in preds.items():
        c, m = Counter(), Counter()
        for aid in ids:
            c["기사|대상"] += 1
            ext, status, meta = load_pred(d, aid, include_review)
            m[f"prompt_version={meta.get('prompt_version')}"] += 1
            m[f"labeler={meta.get('labeler')}"] += 1
            row = {"backend": name, "article_id": aid, "status": status}
            if ext is None:
                c["기사|" + {"review": "예측 없음(검토 대기)", "missing": "예측 없음(파일 없음)", "schema_fail": "스키마 실패"}[status]] += 1
                details.append(row)
                continue
            c["기사|비교"] += 1
            if status == "review_compared":
                c["기사|검토 대기 포함 비교"] += 1
            if length_rule:
                c["평가 쪽 보정|기장 롱→맥시"] += apply_length_rule(ext)
            row.update(compare_article(gold[aid], ext, c, (texts or {}).get(aid)))
            details.append(row)
        counts[name], metas[name] = c, m
    return counts, details, metas


def render_report(counts: dict[str, Counter], meta: dict) -> str:
    names = list(counts)
    L = ["# 기사 추출 비교 (건수)", "",
         f"- split `{meta['split']}` · 기사 {len(meta['ids'])}건 · git `{meta['git_head']}` · {meta['ts']}",
         f"- 예측: " + " · ".join(f"`{k}` = {v}" for k, v in meta["pred"].items()),
         f"- 예측 메타: " + " / ".join(f"`{k}` " + ", ".join(f"{x} ×{n}" for x, n in sorted(v.items())) for k, v in meta["pred_meta"].items()),
         f"- 기장 규칙(L-맥시, D-38로 폐기 — 재현용): {'적용' if meta['length_rule'] else '끔(롱은 롱)'} · `_review`: {'비교 포함' if meta['include_review'] else '예측 없음으로 셈'}",
         "- 규칙: 비율이 아니라 건수다(eval/README). 한 칸 = '이름 건수'.", "",
         "| 지표 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for g, cols in GROUPS:
        cells = [" · ".join(f"{col} {counts[n][f'{g}|{col}']}" for col in cols) for n in names]
        L.append(f"| {g} | " + " | ".join(cells) + " |")
    return "\n".join(L) + "\n"


def parse_pred(specs: list[str]) -> dict[str, Path]:
    out = {}
    for s in specs:
        name, _, path = s.partition("=") if "=" in s else (Path(s).name, "", s)
        p = Path(path).expanduser()
        if not p.is_dir():
            raise SystemExit(f"예측 디렉터리가 없음: {p}")
        out[name] = p
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", action="append", required=True, help="backend=디렉터리 (반복 가능)")
    ap.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    ap.add_argument("--ids", help="쉼표로 구분한 기사 id (split 대신)")
    ap.add_argument("--labels", type=Path, default=LABELS)
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--include-review", action="store_true", help="_review/ 예측도 비교 (진단용)")
    ap.add_argument("--length-rule", action="store_true", help="폐기된 L-맥시(롱→맥시) 보정을 켠다 — 2026-11-10 이전 보고서 재현용 (D-38)")
    ap.add_argument("--no-length-rule", action="store_true", help=argparse.SUPPRESS)  # 예전 명령 호환. 이제 기본과 같다
    ap.add_argument("--confirm-test", action="store_true", help="test 기사를 연다 (열람 기록이 남는다)")
    ap.add_argument("--access-log", type=Path, default=ROOT / "eval" / "results" / "test_access.jsonl")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)

    manifest = load_manifest(a.manifest)
    ids = select_ids(manifest, a.labels, a.split, a.ids.split(",") if a.ids else None)
    preds = parse_pred(a.pred)
    test_ids = needs_test_confirm(manifest, ids)
    if test_ids and not a.confirm_test:
        print(f"test 기사 {len(test_ids)}건이 포함됩니다. 프롬프트 동결 후 최종 평가에서만 --confirm-test로 여세요.", file=sys.stderr)
        return 2
    gold = load_gold(ids, a.labels)
    articles = VL.load_articles()
    texts = {i: VL.text_blocks(articles[i]) for i in ids if i in articles}
    counts, details, metas = run(preds, ids, gold, a.include_review, a.length_rule and not a.no_length_rule, texts)
    if test_ids:
        log_test_access(a.access_log, preds, test_ids, {k: dict(v) for k, v in metas.items()})
    ts = dt.datetime.now().astimezone()
    out = a.out or ROOT / "eval" / "results" / "extraction" / f"{ts:%Y%m%d-%H%M%S}-{a.split if not a.ids else 'ids'}"
    out.mkdir(parents=True, exist_ok=True)
    meta = {"ts": ts.isoformat(timespec="seconds"), "split": a.split if not a.ids else "ids", "ids": ids, "git_head": git_head(),
            "pred": {k: str(v) for k, v in preds.items()}, "pred_meta": {k: dict(v) for k, v in metas.items()},
            "include_review": a.include_review, "length_rule": a.length_rule and not a.no_length_rule, "labels": str(a.labels),
            "thresholds": {"strict_sim": STRICT_SIM, "relaxed_sim": RELAXED_SIM, "style_dice": STYLE_DICE}}
    (out / "report.md").write_text(render_report(counts, meta), encoding="utf-8")
    with (out / "details.jsonl").open("w", encoding="utf-8") as f:
        for row in details:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / "summary.json").write_text(json.dumps({"meta": meta, "counts": {k: dict(v) for k, v in counts.items()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}/report.md · details.jsonl · summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
