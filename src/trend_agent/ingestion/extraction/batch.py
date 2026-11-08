"""기사 일괄 추출 — `scripts/extract_articles.py`의 본체.

- 출력(계약 §5.1): `<out>/<article_id>.json`(검증 통과) · `<out>/_review/<article_id>.json`(1회 재시도 후에도 오류)
  + `<review_queue>`(기본 `<out>/../review_queue.jsonl`) 1줄 · `<out>/run.log`(기사별 JSONL) · `<out>/run_report.md`
- 재개: 출력이나 `_review/`에 이미 있는 article_id는 건너뛴다(`--retry-review`면 `_review/`만 다시).
- 동시성 ≤ 2(구독 경로 보호). 백엔드 오류가 연속 `max_consecutive_failures`번이면 새 기사를 더 시작하지 않는다
  (구독 한도·인증 문제 → 중단·보고).
- LLM 호출 실패 기사는 파일을 쓰지 않는다(다음 실행에서 다시 시도). run.log에 `status: "failed"`.
"""
from __future__ import annotations

import json
import os
import statistics
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from ...llm import LLMError
from .extractor import KST, ExtractionOutcome, build_output_doc, extract_article_outcome

MAX_CONCURRENCY = 2
STAGES = ("V1", "V2", "V3", "V4", "V6")

__all__ = ["BatchConfig", "run_batch", "load_articles", "write_report", "MAX_CONCURRENCY"]


@dataclass
class BatchConfig:
    input_path: Path
    out_dir: Path
    backend: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    prompt_version: str = "extract-v1"
    concurrency: int = 2
    ids: Optional[list[str]] = None
    limit: Optional[int] = None
    retry_review: bool = False
    use_cache: bool = True
    timeout_s: Optional[float] = None
    review_queue: Optional[Path] = None
    max_consecutive_failures: int = 3
    max_text_gap: int = 1
    max_blocks: int = 2


@dataclass
class BatchSummary:
    total: int = 0
    skipped: int = 0
    passed: int = 0
    review: int = 0
    failed: int = 0
    stopped_early: bool = False
    wall_s: float = 0.0
    failures: list[dict] = field(default_factory=list)


def load_articles(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


_lock = threading.Lock()


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _select(articles: list[dict], cfg: BatchConfig) -> tuple[list[dict], int]:
    out, review = cfg.out_dir, cfg.out_dir / "_review"
    chosen = articles
    if cfg.ids:
        wanted = [str(i) for i in cfg.ids]
        by_id = {str(a["article_id"]): a for a in articles}
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise SystemExit(f"입력에 없는 article_id: {missing}")
        chosen = [by_id[i] for i in wanted]
    todo, skipped = [], 0
    for a in chosen:
        aid = str(a["article_id"])
        done = (out / f"{aid}.json").exists()
        in_review = (review / f"{aid}.json").exists()
        if done or (in_review and not cfg.retry_review):
            skipped += 1
            continue
        todo.append(a)
    if cfg.limit is not None:
        todo = todo[: cfg.limit]
    return todo, skipped


def _log_record(out: ExtractionOutcome, status: str, wall_s: float) -> dict:
    ext = out.extraction
    return {
        "ts": _now(), "article_id": out.article_id, "status": status, "attempts": out.attempts,
        "wall_s": round(wall_s, 2), "latency_s": [c.get("latency_s") for c in out.calls],
        "cache_hit": [bool(c.get("cache_hit")) for c in out.calls],
        "usage": [c.get("usage") for c in out.calls], "tool_events": sum(int(c.get("tool_events") or 0) for c in out.calls),
        "counts": out.counts, "n_errors": len(out.errors), "n_warnings": len(out.warnings),
        "n_styles": len(ext.styles) if ext else None,
        "n_items": sum(len(s.items) for s in ext.styles) if ext else None,
    }


def run_batch(cfg: BatchConfig, *, articles: Optional[list[dict]] = None,
              progress: Optional[Callable[[str], None]] = None) -> BatchSummary:
    say = progress or (lambda s: None)
    if cfg.concurrency < 1 or cfg.concurrency > MAX_CONCURRENCY:
        raise SystemExit(f"--concurrency는 1~{MAX_CONCURRENCY}")
    articles = articles if articles is not None else load_articles(cfg.input_path)
    todo, skipped = _select(articles, cfg)
    review_queue = cfg.review_queue or (cfg.out_dir.parent / "review_queue.jsonl")
    log_path = cfg.out_dir / "run.log"
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    summ = BatchSummary(total=len(todo) + skipped, skipped=skipped)
    say(f"대상 {len(todo)}건 · 건너뜀 {skipped}건 · 동시성 {cfg.concurrency} · prompt {cfg.prompt_version}")
    stop = threading.Event()
    consecutive = [0]
    t_start = time.monotonic()

    def work(article: dict) -> None:
        aid = str(article["article_id"])
        if stop.is_set():
            return
        t0 = time.monotonic()
        try:
            out = extract_article_outcome(article, backend=cfg.backend, model=cfg.model,
                                          prompt_version=cfg.prompt_version, reasoning_effort=cfg.reasoning_effort,
                                          timeout_s=cfg.timeout_s, use_cache=cfg.use_cache,
                                          max_blocks=cfg.max_blocks, max_text_gap=cfg.max_text_gap)
        except LLMError as e:
            with _lock:
                consecutive[0] += 1
                summ.failed += 1
                summ.failures.append({"article_id": aid, "error_type": type(e).__name__, "error": str(e)[:300]})
                if consecutive[0] >= cfg.max_consecutive_failures:
                    stop.set()
            _append(log_path, {"ts": _now(), "article_id": aid, "status": "failed", "error_type": type(e).__name__,
                               "error": str(e)[:500], "wall_s": round(time.monotonic() - t0, 2)})
            say(f"{aid}: 실패 {type(e).__name__}: {str(e)[:160]}")
            return
        with _lock:
            consecutive[0] = 0
        doc = build_output_doc(out, article)
        if out.passed:
            _atomic_json(cfg.out_dir / f"{aid}.json", doc)
            stale = cfg.out_dir / "_review" / f"{aid}.json"
            if stale.exists():
                stale.unlink()
            status = "passed"
            with _lock:
                summ.passed += 1
        else:
            _atomic_json(cfg.out_dir / "_review" / f"{aid}.json", doc)
            _append(review_queue, {"ts": _now(), "article_id": aid, "prompt_version": cfg.prompt_version,
                                   "path": str(cfg.out_dir / "_review" / f"{aid}.json"),
                                   "n_errors": len(out.errors), "errors": out.errors[:10]})
            status = "review"
            with _lock:
                summ.review += 1
        rec = _log_record(out, status, time.monotonic() - t0)
        _append(log_path, rec)
        say(f"{aid}: {status} (시도 {out.attempts}, {rec['wall_s']}s, Style {rec['n_styles']}, Item {rec['n_items']})")

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs = [ex.submit(work, a) for a in todo]
        for f in as_completed(futs):
            f.result()
    summ.stopped_early = stop.is_set()
    summ.wall_s = round(time.monotonic() - t_start, 1)
    write_report(cfg.out_dir, prompt_version=cfg.prompt_version, last_run=summ, review_queue=review_queue)
    return summ


# ---------- 보고서 ----------
def _read_docs(folder: Path) -> list[dict]:
    docs = []
    for p in sorted(folder.glob("*.json")):
        try:
            docs.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return docs


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _fmt_table(header: Iterable[str], rows: Iterable[Iterable]) -> str:
    h = list(header)
    lines = ["| " + " | ".join(h) + " |", "|" + "---|" * len(h)]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def write_report(out_dir: Path, *, prompt_version: str, last_run: Optional[BatchSummary] = None,
                 review_queue: Optional[Path] = None) -> Path:
    """폴더의 현재 출력 전체로 `run_report.md`를 다시 쓴다(집계만 — 기사 원문·인용 없음)."""
    passed_docs = _read_docs(out_dir)
    review_docs = _read_docs(out_dir / "_review")
    log = _read_log(out_dir / "run.log")
    latest: dict[str, dict] = {}
    for r in log:
        latest[r["article_id"]] = r
    docs = passed_docs + review_docs
    n = len(docs)

    # 단계별 (최종 시도 기준) 오류 기사 수
    stage_fail = Counter()
    for d in docs:
        counts = (d["label_meta"].get("validation") or {}).get("counts") or {}
        for s in STAGES:
            if counts.get(s):
                stage_fail[s] += 1
    attempts = Counter((d["label_meta"].get("validation") or {}).get("attempts") for d in docs)
    styles_per = Counter()
    items_cat = Counter()
    major_cat = Counter()
    scope = Counter()
    focus = Counter()
    n_style = n_item = n_neg = 0
    flags = Counter()
    for d in passed_docs:
        ext = d["extraction"]
        k = len(ext.get("styles") or [])
        styles_per[k] += 1
        n_style += k
        n_neg += k == 0
        for s in ext.get("styles") or []:
            for f in s.get("focus_attributes") or []:
                focus[(f["attribute"], f["level"])] += 1
            for it in s.get("items") or []:
                n_item += 1
                items_cat[it.get("category_minor") or "(없음)"] += 1
                major_cat[it.get("category_major") or "지원 밖"] += 1
                scope["그래프 범위" if it.get("category_major") else "지원 밖"] += 1
                for fl in it.get("review_flags") or []:
                    flags[fl.split(":", 1)[0]] += 1
    lat = [x for r in latest.values() for x in (r.get("latency_s") or []) if x]
    walls = [r["wall_s"] for r in latest.values() if r.get("wall_s") and r.get("status") != "failed"]
    tok_in = sum((u or {}).get("input_tokens", 0) for r in latest.values() for u in (r.get("usage") or []))
    tok_out = sum((u or {}).get("output_tokens", 0) for r in latest.values() for u in (r.get("usage") or []))
    cache_hits = sum(1 for r in latest.values() for c in (r.get("cache_hit") or []) if c)
    calls = sum(len(r.get("cache_hit") or []) for r in latest.values())
    tools = sum(int(r.get("tool_events") or 0) for r in latest.values())
    failed_now = sorted(a for a, r in latest.items() if r.get("status") == "failed"
                        and not (out_dir / f"{a}.json").exists() and not (out_dir / "_review" / f"{a}.json").exists())
    models = Counter(d["label_meta"].get("labeler") for d in docs)
    efforts = Counter((d["label_meta"].get("llm") or {}).get("reasoning_effort") for d in docs)

    parts = [
        f"# 추출 실행 보고 — `{prompt_version}`",
        "",
        f"- 갱신: {_now()} · 폴더: `{out_dir}`",
        f"- 기사: 통과 **{len(passed_docs)}** · 검토 대기 **{len(review_docs)}** · 호출 실패(파일 없음) **{len(failed_now)}**"
        + (f" {failed_now}" if failed_now else ""),
        f"- 라벨러: {dict(models)} · reasoning effort: {dict(efforts)}",
    ]
    if last_run is not None:
        parts.append(f"- 이번 실행: 대상 {last_run.total - last_run.skipped} · 건너뜀 {last_run.skipped} · 통과 {last_run.passed}"
                     f" · 검토 {last_run.review} · 실패 {last_run.failed} · 벽시계 {last_run.wall_s}s"
                     + (" · **연속 실패로 조기 중단**" if last_run.stopped_early else ""))
    parts += ["", "## 검증 (최종 시도 기준, 기사 수)", "",
              _fmt_table(["단계", "통과", "실패"], [(s, n - stage_fail[s], stage_fail[s]) for s in STAGES]),
              "", f"- 시도 횟수 분포: {dict(sorted(attempts.items()))} (2 = 1회 재시도)",
              f"- 검토 대기열: `{review_queue}`" if review_queue else "",
              "", "## 추출 분포 (통과 기사)", "",
              f"- Style {n_style} · Item {n_item} · 음성(Style 0) 기사 {n_neg}",
              f"- 기사당 Style 수: {dict(sorted(styles_per.items()))}",
              f"- Item 상분류: {dict(major_cat.most_common())}",
              f"- Item 하분류: {dict(items_cat.most_common())}",
              f"- focus (속성, 등급): {dict((f'{a}/{l}', c) for (a, l), c in focus.most_common())}",
              f"- review_flags 종류: {dict(flags.most_common())}",
              "", "## 호출", "",
              f"- 호출 {calls}회 · 캐시 적중 {cache_hits} · 도구 이벤트 {tools}",
              f"- LLM 지연(호출당): 평균 {statistics.mean(lat):.1f}s · 중앙 {statistics.median(lat):.1f}s · 최대 {max(lat):.1f}s"
              if lat else "- LLM 지연: (기록 없음)",
              f"- 기사당 벽시계: 평균 {statistics.mean(walls):.1f}s · 합계 {sum(walls):.0f}s" if walls else "",
              f"- 토큰(최신 실행 기록 합): 입력 {tok_in:,} · 출력 {tok_out:,}", ""]
    path = out_dir / "run_report.md"
    path.write_text("\n".join(x for x in parts if x is not None) + "\n", encoding="utf-8")
    return path
