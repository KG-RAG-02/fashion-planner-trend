"""기사 추출 실험 실행기 — 조건 하나(프롬프트 버전 × 모델)를 기사 몇 건에 돌리고, 결과와 함께 **무엇으로 돌렸는지**를 기록한다.

실행 예 (레포 루트, .venv):
    python scripts/run_experiment.py --prompt-version extract-v4 --model gpt-4o-mini --note "Item 규칙 완화 효과"
    python scripts/run_experiment.py --prompt-version extract-v3 --ids 876706,881396

- 기사: 기본은 로컬 정답 라벨이 있는 기사 전부(eval/datasets/article_extraction/labels/*.json, dev). `--ids`로 바꾼다.
- **실제 OpenAI 호출이다(기사 원문이 나간다).** 실행 전에 사용자 확인을 받는다. 같은 조건·같은 입력은 LLM 캐시를 쓰므로 다시 호출하지 않는다.
- 출력: `data/processed/experiments/<vN>_<model>_<YYYYMMDD-HHMMSS>/` (git 제외. Vogue 원문·인용이 들어 있다)
    <vN>_<article_id>.json            검증 통과한 추출 결과       _review/<vN>_<article_id>.json   검증 실패(검토 대기)
    <vN>_EXPERIMENT.md / .json       실험 기록: 목적·조건·재현 명령·코드·프롬프트·기사별 결과
    <vN>_prompt_template.md          사용한 프롬프트 템플릿 원본   <vN>_prompt_diff_vs_<base>.diff  앞머리 base 대비 차이
    <vN>_llm_schema.json             LLM에 보낸 strict JSON 스키마
    <vN>_code.patch                  실행 시점 git HEAD 대비 미커밋 변경(추적 파일)
    llm_responses/<vN>_<id>_try<k>.json  LLM 원 응답(요청 프롬프트 포함, 캐시 사본)
    <vN>_compare/                    정답 라벨 비교(scripts/compare_extraction.py --include-review): report.md·details.jsonl·summary.json
    <vN>_run_report.md · <vN>_run.log · <vN>_review_queue.jsonl   일괄 실행기 보고·로그
  실험 목록은 `data/processed/experiments/INDEX.md`에 한 줄씩 쌓인다.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.ingestion.extraction.batch import BatchConfig, run_batch  # noqa: E402
from trend_agent.ingestion.extraction.llm_schema import extraction_schema  # noqa: E402
from trend_agent.ingestion.extraction.prompt import PROMPT_DIR, load_template  # noqa: E402
from trend_agent.llm.cache import ResponseCache, default_cache_dir  # noqa: E402
from trend_agent.llm.client import resolve_settings  # noqa: E402

KST = timezone(timedelta(hours=9))
DEFAULT_IN = ROOT / "data" / "raw" / "vogue_kr" / "articles_pool_150.jsonl"
DEFAULT_ROOT = ROOT / "data" / "processed" / "experiments"
LABELS = ROOT / "eval" / "datasets" / "article_extraction" / "labels"
PRICING = ROOT / "configs" / "llm_pricing.yaml"
# 결과에 영향을 주는 코드·설정 (해시를 기록한다)
CODE_GLOBS = ("src/trend_agent/ingestion/extraction/*.py", "src/trend_agent/llm/*.py",
              "src/trend_agent/schemas/article_extraction.py", "src/trend_agent/schemas/vocabulary.py",
              "src/trend_agent/ingestion/normalization/alias_map.py", "configs/alias_map/*.yaml",
              "scripts/run_experiment.py", "scripts/compare_extraction.py", "scripts/validate_labels.py")


def vtag(prompt_version: str) -> str:
    """extract-v4 → v4 (결과 접두어)."""
    return prompt_version.split("extract-", 1)[-1]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def code_info(run: Path, tag: str) -> dict:
    patch = _git("diff", "HEAD")
    (run / f"{tag}_code.patch").write_text(patch, encoding="utf-8", newline="")
    files = sorted({p for g in CODE_GLOBS for p in ROOT.glob(g) if p.is_file()})
    return {
        "git_head": _git("rev-parse", "HEAD").strip(),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "git_status": [ln for ln in _git("status", "--porcelain").splitlines() if ln.strip()],
        "patch_file": f"{tag}_code.patch",
        "file_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): sha256_file(p) for p in files},
        "python": platform.python_version(),
    }


def prompt_info(run: Path, tag: str, prompt_version: str) -> dict:
    _body, front = load_template(prompt_version)
    src = PROMPT_DIR / f"{tag}.md"
    shutil.copyfile(src, run / f"{tag}_prompt_template.md")
    info = {"prompt_version": prompt_version, "template": str(src.relative_to(ROOT)).replace("\\", "/"),
            "template_sha256": sha256_file(src), "copy": f"{tag}_prompt_template.md",
            "guideline_version": front.get("guideline_version"), "changes": front.get("changes"),
            "base": front.get("base"), "season_candidates": bool(front.get("season_candidates"))}
    if front.get("base"):
        btag = vtag(front["base"])
        base = PROMPT_DIR / f"{btag}.md"
        diff = difflib.unified_diff(base.read_text(encoding="utf-8").splitlines(keepends=True),
                                    src.read_text(encoding="utf-8").splitlines(keepends=True),
                                    fromfile=f"{btag}.md", tofile=f"{tag}.md")
        name = f"{tag}_prompt_diff_vs_{btag}.diff"
        (run / name).write_text("".join(diff), encoding="utf-8", newline="")
        info["diff_vs_base"] = name
    return info


def label_ids(labels: Path) -> list[str]:
    return sorted(p.stem for p in labels.glob("*.json"))


def _tokens(usages: list) -> tuple[int, int, int]:
    """(입력, 그중 캐시 적중 입력, 출력). 캐시 적중(=LLM 응답 캐시로 호출 안 함)은 usage가 없어 0이다."""
    i = c = o = 0
    for u in usages or []:
        u = u or {}
        i += int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        c += int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        o += int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    return i, c, o


def load_pricing(path: Path = PRICING) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def cost_usd(model: str, tin: int, tcached: int, tout: int, pricing: dict) -> float | None:
    """OpenAI 단가로 비용(USD). 추론 토큰은 completion_tokens에 들어 있다. 단가가 없으면 None."""
    p = (pricing.get("models") or {}).get(model)
    if not p:
        return None
    pin, pcached, pout = p
    return round(((tin - tcached) * pin + tcached * pcached + tout * pout) / 1e6, 6)


def collect(run: Path, tag: str, ids: list[str], labels: Path) -> list[dict]:
    rows = []
    for aid in ids:
        ok, rv = run / f"{tag}_{aid}.json", run / "_review" / f"{tag}_{aid}.json"
        path = ok if ok.exists() else rv if rv.exists() else None
        row = {"article_id": aid, "status": "passed" if path == ok else "review" if path else "failed(no file)"}
        lab = labels / f"{aid}.json"
        if lab.exists():
            ls = json.loads(lab.read_text(encoding="utf-8"))["extraction"]["styles"]
            row["label_styles"] = [s["name"] for s in ls]
            row["label_items"] = sum(len(s.get("items") or []) for s in ls)
        if path:
            doc = json.loads(path.read_text(encoding="utf-8"))
            m, ext = doc["label_meta"], doc.get("extraction") or {}
            st = ext.get("styles") or []
            v = m.get("validation") or {}
            row.update(file=str(path.relative_to(run)).replace("\\", "/"), attempts=v.get("attempts"),
                       error_counts=v.get("counts"), n_errors=len(v.get("errors") or []),
                       styles=[s.get("name") for s in st],
                       seasons=[[f"{x['year']}{x['season']}" for x in s.get("seasons") or []] for s in st],
                       n_items=sum(len(s.get("items") or []) for s in st),
                       thread_ids=(m.get("llm") or {}).get("thread_ids") or [],
                       cache_hit=(m.get("llm") or {}).get("cache_hit"))
            usages = (m.get("llm") or {}).get("usage")
            row["tokens_in"], row["tokens_cached"], row["tokens_out"] = _tokens(usages)
            row["tokens_reasoning"] = sum(int(((u or {}).get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
                                          for u in usages or [])  # 출력 토큰에 포함된 추론 토큰
            row["llm_calls"] = len([h for h in (m.get("llm") or {}).get("cache_hit") or [] if not h])
        rows.append(row)
    return rows


def copy_responses(run: Path, tag: str, rows: list[dict], *, model: str, prompt_version: str) -> int:
    """LLM 캐시에서 이번 결과의 원 응답(thread id로 찾음)을 실험 폴더로 복사한다."""
    folder = ResponseCache(default_cache_dir()).path(backend="openai_api", model=model, reasoning_effort=None,
                                                     prompt_version=prompt_version, key="x").parent
    by_thread = {}
    for p in folder.glob("*.json") if folder.exists() else []:
        try:
            by_thread[json.loads(p.read_text(encoding="utf-8"))["meta"]["thread_id"]] = p
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    out, n = run / "llm_responses", 0
    for r in rows:
        for k, th in enumerate(r.get("thread_ids") or [], 1):
            if th in by_thread:
                out.mkdir(exist_ok=True)
                shutil.copyfile(by_thread[th], out / f"{tag}_{r['article_id']}_try{k}.json")
                n += 1
    return n


def compare(run: Path, tag: str, ids: list[str]) -> str | None:
    labeled = [i for i in ids if (LABELS / f"{i}.json").exists()]
    if not labeled:
        return None
    out = run / f"{tag}_compare"
    cmd = [sys.executable, str(ROOT / "scripts" / "compare_extraction.py"), "--pred", f"{tag}={run}",
           "--ids", ",".join(labeled), "--include-review", "--out", str(out)]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    (run / f"{tag}_compare.log").write_text(res.stdout + res.stderr, encoding="utf-8")
    return f"{tag}_compare/report.md" if (out / "report.md").exists() else f"{tag}_compare.log (실패)"


def write_record(run: Path, tag: str, rec: dict) -> None:
    (run / f"{tag}_EXPERIMENT.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    c, p, r = rec["condition"], rec["prompt"], rec["results"]
    lines = [
        f"# 실험 `{rec['run_id']}`", "",
        f"- 시각: {rec['started_at']} ~ {rec['finished_at']}",
        f"- 목적·메모: {rec.get('note') or '(없음)'}", "",
        "## 조건", "",
        "| 항목 | 값 |", "|---|---|",
        f"| 프롬프트 | `{p['prompt_version']}` (`{p['template']}`, sha256 `{p['template_sha256'][:12]}`) |",
        f"| 프롬프트 변경 | {p.get('changes') or ''} |",
        f"| 기준(base) 대비 diff | {p.get('diff_vs_base') or '없음'} |",
        (f"| 모델 | `{c['backend']}` / `{c['model']}` — 추론 모델, temperature 없음, 추론 강도 "
         f"`{c.get('reasoning_effort') or 'API 기본값'}` (Structured Outputs strict, `llm/backends.py`, D-30) |"
         if c.get("reasoning_model") else
         f"| 모델 | `{c['backend']}` / `{c['model']}` (temperature 0, Structured Outputs strict — `llm/backends.py`) |"),
        f"| 기사 | {len(c['ids'])}건: {', '.join(c['ids'])} |",
        f"| 입력 | `{c['input']}` (sha256 `{c['input_sha256'][:12]}`) |",
        f"| 실행 설정 | 재시도 1회(검증 오류 시) · 동시성 {c['concurrency']} · 근거 블록 ≤ {c['max_blocks']} · 캐시 {c['use_cache']} |",
        f"| 후처리 | {c.get('postprocess') or {'alias_map': True}} (dedupe = P1 중복 합치기, name_fix = P2 이름 끝 '트렌드' 제거 — docs/pipeline.md §6.7) |",
        f"| LLM 스키마 | `{tag}_llm_schema.json` (sha256 `{c['schema_sha256'][:12]}`) |", "",
        "## 재현", "", "```", rec["command"], "```",
        f"코드: git `{rec['code']['git_head'][:10]}` ({rec['code']['git_branch']}) + 미커밋 변경 `{rec['code']['patch_file']}`. "
        f"결과에 영향을 주는 파일의 sha256은 `{tag}_EXPERIMENT.json`의 `code.file_sha256`.", "",
        "## 결과 (기사별)", "",
        "| 기사 | 상태 | 시도 | Style (라벨) | 시즌 | Item 수 (라벨) | 오류 코드 | 토큰 입력/출력 | 비용 $ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for x in r["articles"]:
        c = x.get("cost_usd")
        lines.append(f"| {x['article_id']} | {x['status']} | {x.get('attempts', '')} | {', '.join(x.get('styles') or []) or '-'}"
                     f" ({', '.join(x.get('label_styles') or [])}) | {x.get('seasons', '')} | {x.get('n_items', '-')}"
                     f" ({x.get('label_items', '-')}) | {x.get('error_counts') or ''} | {x.get('tokens_in', 0)}/{x.get('tokens_out', 0)}"
                     f" | {'' if c is None else f'{c:.4f}'} |")
    s = r["summary"]
    pr = rec.get("pricing") or {}
    cost = f"${s['cost_usd']:.4f}" if s.get("cost_usd") is not None else "단가 미등록"
    lines += ["", f"- 합계: 통과 {s['passed']} · 검토 대기 {s['review']} · 호출 실패 {s['failed']} · 벽시계 {s['wall_s']}s",
              f"- 비용: **{cost}** — 실제 LLM 호출 {s.get('llm_calls', 0)}회 · 토큰 입력 {s['tokens_in']:,}(캐시 할인 {s.get('tokens_cached', 0):,})"
              f" / 출력 {s['tokens_out']:,}(그중 추론 {s.get('tokens_reasoning', 0):,}) · 단가 {pr.get('usd_per_1m')} $/1M (입력·캐시 입력·출력, {pr.get('source')} {pr.get('checked_at')} 확인)."
              " LLM 응답 캐시로 재사용한 호출은 비용 0",
              f"- 라벨 비교: {r.get('compare') or '없음'}",
              f"- 원 응답 사본: `llm_responses/` {r.get('responses_copied', 0)}개", ""]
    (run / f"{tag}_EXPERIMENT.md").write_text("\n".join(lines), encoding="utf-8")


def append_index(root: Path, rec: dict) -> None:
    idx = root / "INDEX.md"
    if not idx.exists():
        idx.write_text("# 추출 실험 목록\n\n| run_id | 프롬프트 | 모델 | 기사 | 통과/검토/실패 | Item 합계(라벨) | 비용 $ | 메모 |\n"
                       "|---|---|---|---|---|---|---|---|\n", encoding="utf-8")
    s, arts = rec["results"]["summary"], rec["results"]["articles"]
    items = sum(a.get("n_items") or 0 for a in arts)
    lab = sum(a.get("label_items") or 0 for a in arts)
    with idx.open("a", encoding="utf-8") as f:
        f.write(f"| [{rec['run_id']}]({rec['run_id']}/{vtag(rec['prompt']['prompt_version'])}_EXPERIMENT.md) | "
                f"{rec['prompt']['prompt_version']} | {rec['condition']['model']} | {len(arts)} | "
                f"{s['passed']}/{s['review']}/{s['failed']} | {items}({lab}) | "
                f"{'' if s.get('cost_usd') is None else round(s['cost_usd'], 4)} | {rec.get('note') or ''} |\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt-version", required=True, help="예 extract-v4")
    ap.add_argument("--model", default=None, help="기본 env LLM_MODEL")
    ap.add_argument("--reasoning-effort", default=None, choices=("minimal", "low", "medium", "high"),
                    help="추론 모델만(gpt-5·o 계열). 주지 않으면 API 기본값. 폴더 이름에 붙는다 (docs/pipeline.md D-30)")
    ap.add_argument("--ids", default=None, help="쉼표로 구분한 article_id (기본: 로컬 라벨 기사 전부)")
    ap.add_argument("--note", default="", help="실험 목적·가설 (기록에 남는다)")
    ap.add_argument("--in", dest="input", type=Path, default=DEFAULT_IN)
    ap.add_argument("--out-root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--no-cache", action="store_true")
    # P1·P2는 채택됐다(2026-11-09, docs/pipeline.md D-27) → 기본 켬. 켜진 후처리는 폴더 이름에 _dedupe·_namefix로 붙는다
    ap.add_argument("--no-dedupe", dest="dedupe", action="store_false",
                    help="P1 중복 focus_attributes·seasons 합치기를 끈다 (기본 켬)")
    ap.add_argument("--no-name-fix", dest="name_fix", action="store_false",
                    help="P2 이름 끝 '트렌드' 제거를 끈다 (기본 켬, docs/pipeline.md §6.7)")
    ap.add_argument("--dedupe", dest="dedupe", action="store_true", help=argparse.SUPPRESS)    # 옛 실행 명령 호환
    ap.add_argument("--name-fix", dest="name_fix", action="store_true", help=argparse.SUPPRESS)
    ap.set_defaults(dedupe=True, name_fix=True)
    a = ap.parse_args(argv)

    tag = vtag(a.prompt_version)
    st = resolve_settings(model=a.model, reasoning_effort=a.reasoning_effort)  # 일반 모델 + 추론 강도면 여기서 오류
    model, effort = st["model"], st["reasoning_effort"]
    ids = [x.strip() for x in a.ids.split(",") if x.strip()] if a.ids else label_ids(LABELS)
    if not ids:
        raise SystemExit("기사가 없다: --ids를 주거나 로컬 라벨을 둔다")
    started = datetime.now(KST)
    pp = ("_dedupe" if a.dedupe else "") + ("_namefix" if a.name_fix else "")
    run_id = f"{tag}_{model.replace('/', '-')}{'-' + effort if effort else ''}{pp}_{started:%Y%m%d-%H%M%S}"
    run = a.out_root / run_id
    run.mkdir(parents=True, exist_ok=False)

    schema, _dropped, schema_sha = extraction_schema()
    (run / f"{tag}_llm_schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")
    rec: dict = {"run_id": run_id, "note": a.note, "started_at": started.isoformat(timespec="seconds"),
                 "command": " ".join(["python", "scripts/run_experiment.py", *(argv if argv is not None else sys.argv[1:])]),
                 "prompt": prompt_info(run, tag, a.prompt_version), "code": code_info(run, tag)}
    cfg = BatchConfig(input_path=a.input, out_dir=run, model=model, prompt_version=a.prompt_version, ids=ids,
                      concurrency=a.concurrency, use_cache=not a.no_cache, file_prefix=f"{tag}_",
                      review_queue=run / f"{tag}_review_queue.jsonl", dedupe=a.dedupe, name_fix=a.name_fix,
                      reasoning_effort=effort)
    from trend_agent.llm.backends import is_reasoning_model
    reasoning = is_reasoning_model(model)
    rec["condition"] = {"backend": "openai_api", "model": model, "reasoning_model": reasoning,
                        "temperature": None if reasoning else 0, "reasoning_effort": effort, "ids": ids,
                        "input": str(a.input.relative_to(ROOT) if a.input.is_relative_to(ROOT) else a.input).replace("\\", "/"),
                        "input_sha256": sha256_file(a.input), "concurrency": cfg.concurrency, "max_blocks": cfg.max_blocks,
                        "max_text_gap": cfg.max_text_gap, "max_retries": 1, "use_cache": cfg.use_cache,
                        "schema_sha256": schema_sha,
                        "postprocess": {"alias_map": True, "length_area": cfg.length_area, "dedupe": cfg.dedupe,
                                        "name_fix": cfg.name_fix}}
    summ = run_batch(cfg, progress=lambda s: print(s, flush=True))
    for name in ("run_report.md", "run.log"):  # 결과 파일 접두어
        if (run / name).exists():
            (run / name).replace(run / f"{tag}_{name}")

    rows = collect(run, tag, ids, LABELS)
    n_copied = copy_responses(run, tag, rows, model=model, prompt_version=a.prompt_version)
    tin = sum(r.get("tokens_in", 0) for r in rows)
    tcached = sum(r.get("tokens_cached", 0) for r in rows)
    tout = sum(r.get("tokens_out", 0) for r in rows)
    pricing = load_pricing()
    for r in rows:
        r["cost_usd"] = cost_usd(model, r.get("tokens_in", 0), r.get("tokens_cached", 0), r.get("tokens_out", 0), pricing)
    rec["pricing"] = {"source": pricing.get("source"), "checked_at": pricing.get("checked_at"),
                      "usd_per_1m": (pricing.get("models") or {}).get(model)}
    rec["results"] = {"articles": rows, "responses_copied": n_copied, "compare": compare(run, tag, ids),
                      "summary": {"passed": summ.passed, "review": summ.review, "failed": summ.failed,
                                  "skipped": summ.skipped, "stopped_early": summ.stopped_early, "wall_s": summ.wall_s,
                                  "llm_calls": sum(r.get("llm_calls", 0) for r in rows),
                                  "tokens_in": tin, "tokens_cached": tcached, "tokens_out": tout,
                                  "tokens_reasoning": sum(r.get("tokens_reasoning", 0) for r in rows),
                                  "cost_usd": cost_usd(model, tin, tcached, tout, pricing),
                                  "failures": summ.failures}}
    rec["finished_at"] = datetime.now(KST).isoformat(timespec="seconds")
    write_record(run, tag, rec)
    append_index(a.out_root, rec)
    print(f"실험 {run_id}: 통과 {summ.passed} · 검토 {summ.review} · 실패 {summ.failed} → {run / (tag + '_EXPERIMENT.md')}")
    return 3 if summ.stopped_early else (2 if summ.failed else 0)


if __name__ == "__main__":
    sys.exit(main())
