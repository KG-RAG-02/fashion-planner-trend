"""기사 추출 정답(label) 검증. 문서: eval/guidelines/article_extraction.md §8

검사:
  1. label_meta 필수 필드와 status(silver|gold)
  2. `extraction`이 ArticleExtraction 스키마를 통과하는가 (구조·허용 값·핵심 속성 규칙)
  3. 모든 evidence의 quote가 해당 block_idx 문단에 실제로 있는가 (공백을 뺀 부분 문자열)
  4. 모든 *_raw 값이 그 Item의 근거 문단 안에 있는가 (경고)
  5. guideline_version이 매니페스트와 같은가 (경고)

실행:  python scripts/validate_labels.py [article_id ...]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.schemas.article_extraction import ArticleExtraction  # noqa: E402

LABELS = ROOT / "eval" / "datasets" / "article_extraction" / "labels"
MANIFEST = ROOT / "eval" / "datasets" / "article_extraction" / "manifest.yaml"
RAW = ROOT / "data" / "raw" / "vogue_kr"
META_FIELDS = ("article_id", "url", "title", "status", "labeler", "reviewer", "guideline_version", "labeled_at", "notes")


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def load_articles() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for p in RAW.glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            rows.setdefault(r["article_id"], r)
    return rows


def manifest_version() -> str | None:
    text = MANIFEST.read_text(encoding="utf-8")
    return yaml.safe_load(text[text.index("guideline_version"):]).get("guideline_version")


def text_blocks(article: dict) -> dict[int, str]:
    """기사 원본 → {block idx: text} (이미지 블록 제외 = 문단·소제목). V2·V3 검사의 입력."""
    return {b["idx"]: b.get("text", "") for b in article["blocks"] if b["type"] != "image"}


def _quote_errors(ext: ArticleExtraction, text_by_idx: dict[int, str], require_item_evidence: bool) -> list[str]:
    errors: list[str] = []

    def check_quote(where: str, block_idx: int, quote: str) -> None:
        text = text_by_idx.get(block_idx)
        if text is None:
            errors.append(f"{where}: block_idx {block_idx}에 문단이 없음")
        elif squash(quote) not in squash(text):
            errors.append(f"{where}: quote가 block {block_idx}에 없음: {quote[:40]}")

    for s in ext.styles:
        for e in s.evidence:
            check_quote(f"Style[{s.name}].evidence", e.block_idx, e.quote)
        for d in s.description:
            for e in d.evidence:
                check_quote(f"Style[{s.name}].description", e.block_idx, e.quote)
        for f in s.focus_attributes:
            for e in f.evidence:
                check_quote(f"Style[{s.name}].focus[{f.attribute.value}]", e.block_idx, e.quote)
        for it in s.items:
            if require_item_evidence and not it.evidence:
                errors.append(f"Item[{it.name}]: evidence 없음")
            for e in it.evidence:
                check_quote(f"Item[{it.name}].evidence", e.block_idx, e.quote)
    return errors


def item_raws(it) -> list[str | None]:
    """Item의 원문 표현 필드(*_raw, 하위 객체 raw) — V3 검사 대상."""
    raws = [it.category_raw, it.fit_raw, it.shape_raw, it.length_body_raw, it.length_sleeve_raw, it.styling_raw, it.styling_accessory]
    return raws + [c.raw for c in it.colors] + [m.raw for m in it.materials] + [p.raw for p in it.patterns] + [d.raw for d in it.details]


def check_quotes(ext: ArticleExtraction, text_by_idx: dict[int, str]) -> list[str]:
    """V2 근거 존재: 모든 quote가 해당 block_idx의 텍스트 블록(문단·소제목)에 있는가 (공백 무시 부분 문자열). 오류 문구 목록."""
    return _quote_errors(ext, text_by_idx, require_item_evidence=False)


def check_raws(ext: ArticleExtraction, text_by_idx: dict[int, str]) -> list[str]:
    """V3 속성 존재: Item의 모든 *_raw·하위 raw가 그 Item 근거 블록들 안에 있는가. 경고 문구 목록."""
    warnings: list[str] = []
    for s in ext.styles:
        for it in s.items:
            ev_text = squash("".join(text_by_idx.get(e.block_idx, "") for e in it.evidence))
            for r in item_raws(it):
                if r and squash(r) not in ev_text:
                    warnings.append(f"Item[{it.name}]: raw '{r}'가 근거 문단에 없음 (지시 표현이면 notes에 적을 것)")
    return warnings


def validate(path: Path, articles: dict[str, dict], version: str | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    doc = json.loads(path.read_text(encoding="utf-8"))
    meta = doc.get("label_meta", {})
    for f in META_FIELDS:
        if f not in meta:
            errors.append(f"label_meta.{f} 없음")
    if meta.get("status") not in ("silver", "gold"):
        errors.append(f"status가 silver|gold가 아님: {meta.get('status')}")
    if meta.get("article_id") != path.stem:
        errors.append("파일 이름과 label_meta.article_id가 다름")
    if version and meta.get("guideline_version") != version:
        warnings.append(f"guideline_version {meta.get('guideline_version')} != manifest {version}")
    try:
        ext = ArticleExtraction.model_validate(doc["extraction"])
    except Exception as e:  # pydantic ValidationError
        return [f"스키마 오류: {e}"] + errors, warnings
    art = articles.get(path.stem)
    if art is None:
        return errors + ["원본 기사를 찾을 수 없음 (data/raw/vogue_kr)"], warnings
    text_by_idx = text_blocks(art)
    errors += _quote_errors(ext, text_by_idx, require_item_evidence=True)
    warnings += check_raws(ext, text_by_idx)
    return errors, warnings


def main() -> int:
    ids = sys.argv[1:] or sorted(p.stem for p in LABELS.glob("*.json"))
    if not ids:
        print("라벨 파일이 없습니다.")
        return 0
    articles, version = load_articles(), manifest_version()
    bad = 0
    for i in ids:
        errs, warns = validate(LABELS / f"{i}.json", articles, version)
        status = "OK" if not errs else "FAIL"
        print(f"{i}: {status} (errors {len(errs)}, warnings {len(warns)})")
        for e in errs:
            print("   ERROR", e)
        for w in warns:
            print("   warn ", w)
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
