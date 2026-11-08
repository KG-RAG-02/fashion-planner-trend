"""기사 1건 추출: 프롬프트 → LLM(strict 스키마) → 후처리(이미지 번호·별칭 사전) → pydantic → V1~V6 → (오류면) 1회 재시도.

    extract_article(article, *, backend, model, prompt_version) -> ArticleExtraction   # 검증 실패면 ExtractionFailed
    extract_article_outcome(article, *, backend, model, prompt_version, ...) -> ExtractionOutcome
    build_output_doc(outcome, article) -> {"label_meta": …, "extraction": …}             # 계약 §5.1 auto 파일

LLM 호출 실패(백엔드·시간 초과)는 재시도하지 않고 `LLMError`를 그대로 올린다(일괄 스크립트가 기록·재개).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import ValidationError

from ... import llm
from ...schemas.article_extraction import ArticleExtraction
from .llm_schema import extraction_schema
from .postprocess import postprocess
from .prompt import load_template, render_prompt, render_retry_suffix
from .validate import ValidationReport, validate_extraction

PURPOSE = "article_extraction"
KST = timezone(timedelta(hours=9))

__all__ = ["ExtractionOutcome", "ExtractionFailed", "extract_article", "extract_article_outcome", "build_output_doc"]


@dataclass
class ExtractionOutcome:
    article_id: str
    extraction: Optional[ArticleExtraction]
    raw_output: Optional[dict]          # 마지막 시도의 후처리 dict (pydantic 실패 시 검토용)
    passed: bool
    attempts: int
    errors: list[str]
    warnings: list[str]
    counts: dict[str, int]
    calls: list[dict] = field(default_factory=list)  # llm meta (원문 없음)
    backend: str = ""
    model: str = ""
    reasoning_effort: Optional[str] = None
    prompt_version: str = ""
    guideline_version: Optional[str] = None
    schema_sha256: str = ""
    extracted_at: str = ""


class ExtractionFailed(RuntimeError):
    def __init__(self, outcome: ExtractionOutcome):
        super().__init__(f"{outcome.article_id}: 검증 실패 {len(outcome.errors)}건 (시도 {outcome.attempts})")
        self.outcome = outcome


def _attempt(article: dict, prompt: str, schema: dict, *, prompt_version: str, backend: Optional[str],
             model: Optional[str], reasoning_effort: Optional[str], timeout_s: Optional[float], use_cache: bool,
             max_blocks: int, max_text_gap: int) -> tuple[Optional[ArticleExtraction], Optional[dict], ValidationReport, dict]:
    res = llm.call(prompt, schema, purpose=PURPOSE, prompt_version=prompt_version, backend=backend, model=model,
                   reasoning_effort=reasoning_effort, timeout_s=timeout_s, use_cache=use_cache)
    doc, v6 = postprocess(res.data, article)
    try:
        ext = ArticleExtraction.model_validate(doc)
    except ValidationError as e:
        rep = ValidationReport()
        for err in e.errors()[:20]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            rep.err("V1", f"pydantic {loc}: {err.get('msg')}")
        for x in v6:
            rep.errors.append(x)
            rep.counts["V6"] = rep.counts.get("V6", 0) + 1
        return None, doc, rep, res.meta
    rep = validate_extraction(ext, article, max_blocks=max_blocks, max_text_gap=max_text_gap, extra_errors=v6)
    return ext, doc, rep, res.meta


def extract_article_outcome(article: dict, *, backend: Optional[str] = None, model: Optional[str] = None,
                            prompt_version: str = "extract-v1", reasoning_effort: Optional[str] = None,
                            timeout_s: Optional[float] = None, use_cache: bool = True, max_retries: int = 1,
                            max_blocks: int = 2, max_text_gap: int = 1) -> ExtractionOutcome:
    schema, _dropped, sha = extraction_schema()
    _tmpl, front = load_template(prompt_version)
    base_prompt = render_prompt(article, prompt_version)
    prompt = base_prompt
    calls: list[dict] = []
    ext = doc = None
    rep = ValidationReport()
    attempts = 0
    for attempt in range(1 + max_retries):
        attempts = attempt + 1
        ext, doc, rep, meta = _attempt(article, prompt, schema, prompt_version=prompt_version, backend=backend,
                                       model=model, reasoning_effort=reasoning_effort, timeout_s=timeout_s,
                                       use_cache=use_cache, max_blocks=max_blocks, max_text_gap=max_text_gap)
        calls.append(meta)
        if rep.passed:
            break
        prompt = base_prompt + render_retry_suffix(rep.errors)
    last = calls[-1] if calls else {}
    return ExtractionOutcome(
        article_id=str(article["article_id"]), extraction=ext, raw_output=doc, passed=rep.passed and ext is not None,
        attempts=attempts, errors=rep.errors, warnings=rep.warnings, counts=rep.counts, calls=calls,
        backend=last.get("backend", ""), model=last.get("model", ""), reasoning_effort=last.get("reasoning_effort"),
        prompt_version=prompt_version, guideline_version=front.get("guideline_version"), schema_sha256=sha,
        extracted_at=datetime.now(KST).isoformat(timespec="seconds"),
    )


def extract_article(article: dict, *, backend: Optional[str] = None, model: Optional[str] = None,
                    prompt_version: str = "extract-v1", **kw: Any) -> ArticleExtraction:
    out = extract_article_outcome(article, backend=backend, model=model, prompt_version=prompt_version, **kw)
    if not out.passed or out.extraction is None:
        raise ExtractionFailed(out)
    return out.extraction


def build_output_doc(out: ExtractionOutcome, article: dict) -> dict:
    """계약 §5.1 자동 추출 파일. 검증 실패(검토 대기)도 같은 형식(`validation.passed = false`)."""
    extraction: Any
    if out.extraction is not None:
        extraction = out.extraction.model_dump(mode="json")
    else:
        extraction = out.raw_output  # pydantic 실패 — 검토용 원형
    return {
        "label_meta": {
            "article_id": out.article_id,
            "url": article.get("url"),
            "title": article.get("title"),
            "published": str(article.get("published_at") or "")[:10] or None,
            "status": "auto",
            "labeler": f"llm:{out.backend}/{out.model}",
            "prompt_version": out.prompt_version,
            "extracted_at": out.extracted_at,
            "guideline_version": out.guideline_version,
            "validation": {"passed": out.passed, "attempts": out.attempts, "errors": out.errors,
                           "warnings": out.warnings, "counts": out.counts},
            "llm": {
                "backend": out.backend, "model": out.model, "reasoning_effort": out.reasoning_effort,
                "thread_ids": [c.get("thread_id") for c in out.calls],
                "latency_s": round(sum((c.get("latency_s") or 0.0) for c in out.calls), 3),
                "cache_hit": [bool(c.get("cache_hit")) for c in out.calls],
                "schema_sha256": out.schema_sha256,
                "usage": [c.get("usage") for c in out.calls],
                "tool_events": sum(int(c.get("tool_events") or 0) for c in out.calls),
            },
        },
        "extraction": extraction,
    }
