"""새 LLM 모델 합성 확인 — 등록표(configs/llm_models.yaml)에 넣기 전에, 기사 없이 합성 문장 1건으로 요청 형식을 확인한다.

실행 (레포 루트, .venv):
    python scripts/check_llm_model.py --model gpt-5-mini --profile reasoning --reasoning-effort medium
    python scripts/check_llm_model.py --model gpt-4.1                     # 이미 등록된 모델 다시 확인 (profile 생략)

- 실제 OpenAI 호출 1회다. 보내는 것은 아래 합성 문장뿐이다(기사·인용 없음 → docs/pipeline.md D-1 범위).
- 캐시를 쓰지 않는다. 호출 기록은 data/processed/llm/calls.jsonl에 `purpose: model_check`로 남는다.
- 등록표 파일은 바꾸지 않는다. 미등록 모델은 이 프로세스 안에서만 임시 등록해 호출한다(`models.register_model`).
- 통과 기준 (docs/pipeline.md §14.2): HTTP 200 + strict 스키마 JSON 응답 + 응답이 스키마와 일치(client가 검사).
  통과하면 출력되는 블록을 configs/llm_models.yaml에 넣고 checked_at을 적는다(사용자에게 알린 뒤).
- 종료 코드: 0 = 통과, 1 = 실패.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent import llm  # noqa: E402
from trend_agent.llm import models  # noqa: E402
from trend_agent.llm.errors import LLMBackendError, LLMError  # noqa: E402

# 기사와 무관한 합성 문장과 작은 strict 스키마
SYNTHETIC_PROMPT = ("다음 합성 문장에서 색과 옷 종류를 뽑아 JSON 객체 하나로만 답하라. 문장에 없는 것은 null 또는 []로 둔다.\n"
                    "문장: \"와인색 코트와 회색 니트를 함께 입었다.\"")
SCHEMA = {
    "type": "object",
    "properties": {
        "colors": {"type": "array", "items": {"type": "string"}},
        "garments": {"type": "array", "items": {"type": "string"}},
        "pattern": {"type": ["string", "null"]},
    },
    "required": ["colors", "garments", "pattern"],
    "additionalProperties": False,
}
PROMPT_VERSION = "model-check-v1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--profile", choices=models.PROFILES, default=None,
                    help="미등록 모델의 계열 (등록된 모델은 등록표 값을 쓴다)")
    ap.add_argument("--efforts", default=None, help="추론 모델의 허용 강도 (쉼표, 기본 minimal,low,medium,high)")
    ap.add_argument("--reasoning-effort", default=None)
    ap.add_argument("--timeout", type=float, default=120.0)
    a = ap.parse_args(argv)

    temporary = False
    try:
        spec = models.get_spec(a.model)
        if a.profile and a.profile != spec.profile:
            print(f"오류: {a.model}은 등록표에 {spec.profile}로 있다 (--profile {a.profile}와 다르다)", file=sys.stderr)
            return 1
    except LLMBackendError:
        if not a.profile:
            print(f"오류: {a.model}은 등록되지 않았다. --profile standard|reasoning을 준다", file=sys.stderr)
            return 1
        efforts = [x.strip() for x in a.efforts.split(",")] if a.efforts else None
        spec = models.register_model(a.model, a.profile, efforts=efforts, note="합성 확인 중(임시)")
        temporary = True
    try:
        res = llm.call(SYNTHETIC_PROMPT, SCHEMA, purpose="model_check", prompt_version=PROMPT_VERSION,
                       model=a.model, reasoning_effort=a.reasoning_effort, timeout_s=a.timeout, use_cache=False)
    except LLMError as e:
        print(f"실패: {a.model} ({spec.profile}) — {type(e).__name__}: {str(e)[:300]}")
        return 1
    finally:
        if temporary:
            models.unregister_model(a.model)
    m = res.meta
    usage = m.get("usage") or {}
    print(f"통과: {a.model} ({spec.profile}, 추론 강도 {m.get('reasoning_effort') or 'API 기본값'}) · "
          f"지연 {m.get('latency_s')}s · 토큰 입력 {usage.get('prompt_tokens')} / 출력 {usage.get('completion_tokens')} "
          f"(추론 {(usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)}) · thread {m.get('thread_id')}")
    print("응답:", json.dumps(res.data, ensure_ascii=False))
    if temporary:
        lines = [f"  {a.model}:", f"    profile: {spec.profile}"]
        if spec.profile == "reasoning":
            lines.append(f"    efforts: [{', '.join(spec.efforts or ('minimal', 'low', 'medium', 'high'))}]")
        lines += [f"    checked_at: \"{date.today().isoformat()}\"", "    note: 합성 확인 통과 (scripts/check_llm_model.py)"]
        print("\nconfigs/llm_models.yaml의 models: 아래에 넣을 항목 (사용자에게 알린 뒤):\n" + "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
