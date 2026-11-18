"""기사 추출 정답(label) 검증. 문서: docs/annotation-guide.md

검사:
  1. label_meta 필수 필드와 status(silver|gold)
  2. `extraction`이 ArticleExtraction 스키마를 통과하는가 (구조·허용 값·핵심 속성 규칙)
  3. 모든 evidence의 quote가 해당 block_idx 문단에 실제로 있는가 (공백을 뺀 부분 문자열)
  4. 모든 *_raw 값이 그 Item의 근거 문단 안에 있는가 (경고)

실행:  python scripts/validate_labels.py [article_id ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fashion_planner_trend.ingestion.extraction.evidence import (  # noqa: E402,F401 — 테스트·호출자가 이 이름으로 쓴다
    check_quotes,
    check_raws,
    quote_errors,
    squash,
    text_blocks,
)
from fashion_planner_trend.schemas.article_extraction import ArticleExtraction  # noqa: E402

LABELS = ROOT / "data" / "labels"
RAW = ROOT / "data" / "raw"
META_FIELDS = ("article_id", "url", "title", "status", "labeler", "reviewer", "labeled_at", "notes")


def load_articles() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for p in RAW.glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            rows.setdefault(r["article_id"], r)
    return rows


def validate(path: Path, articles: dict[str, dict]) -> tuple[list[str], list[str]]:
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
    try:
        ext = ArticleExtraction.model_validate(doc["extraction"])
    except Exception as e:  # pydantic ValidationError
        return [f"스키마 오류: {e}"] + errors, warnings
    art = articles.get(path.stem)
    if art is None:
        return errors + ["원본 기사를 찾을 수 없음 (data/raw)"], warnings
    text_by_idx = text_blocks(art)
    errors += quote_errors(ext, text_by_idx, require_item_evidence=True)
    warnings += check_raws(ext, text_by_idx)
    return errors, warnings


def main() -> int:
    ids = sys.argv[1:] or sorted(p.stem for p in LABELS.glob("*.json"))
    if not ids:
        print("라벨 파일이 없습니다.")
        return 0
    articles = load_articles()
    bad = 0
    for i in ids:
        errs, warns = validate(LABELS / f"{i}.json", articles)
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
