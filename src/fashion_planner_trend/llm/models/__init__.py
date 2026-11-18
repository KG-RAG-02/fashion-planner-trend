"""모델 등록표와 모델 계열 모듈 선택 (docs/extraction.md).

- 등록표: `configs/llm_models.yaml` — 모델별 `profile`(계열), `efforts`, `checked_at`, `note`. 미등록 모델은 오류다.
- 계열 모듈: `standard.py`(일반 모델), `reasoning.py`(추론 모델). 각 모듈은 같은 두 함수를 낸다:
      check_effort(spec: ModelSpec, effort: str | None) -> str | None   # 허용되지 않으면 LLMBackendError
      request_params(effort: str | None) -> dict                        # 요청 본문에 더할 파라미터
- `get_profile(model)`이 등록표를 보고 계열 모듈을 골라 import한다. 새 계열은 모듈을 만들고 `PROFILES`에 넣는다.
- `register_model(...)`은 **프로세스 안에서만** 임시 등록한다(테스트·합성 확인용). 등록표 파일은 바꾸지 않는다.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Optional

import yaml

from ...config import ROOT
from ..errors import LLMBackendError

__all__ = ["ModelSpec", "PROFILES", "REGISTRY_PATH", "load_registry", "get_spec", "get_profile", "register_model",
           "unregister_model"]

REGISTRY_PATH = ROOT / "configs" / "llm_models.yaml"
PROFILES = ("standard", "reasoning")   # 계열 이름 = 이 패키지 안의 모듈 이름


@dataclass(frozen=True)
class ModelSpec:
    name: str
    profile: str
    efforts: tuple[str, ...] = ()
    checked_at: Optional[str] = None
    note: str = ""
    source: str = "registry"   # registry(등록표 파일) | runtime(register_model)


_runtime: dict[str, ModelSpec] = {}


def _spec_from(name: str, raw: dict, *, source: str, require_checked: bool) -> ModelSpec:
    if not isinstance(raw, dict):
        raise LLMBackendError(f"모델 등록표: {name}의 항목이 표가 아니다")
    profile = raw.get("profile")
    if profile not in PROFILES:
        raise LLMBackendError(f"모델 등록표: {name}의 profile은 {PROFILES} 중 하나여야 한다: {profile!r}")
    efforts = tuple(raw.get("efforts") or ())
    if efforts and profile != "reasoning":
        raise LLMBackendError(f"모델 등록표: {name}은 {profile} 모델이라 efforts를 둘 수 없다")
    checked = raw.get("checked_at")
    if require_checked and not checked:
        raise LLMBackendError(f"모델 등록표: {name}에 checked_at(동작 확인일)이 없다")
    return ModelSpec(name=name, profile=profile, efforts=efforts, checked_at=str(checked) if checked else None,
                     note=str(raw.get("note") or ""), source=source)


@lru_cache(maxsize=4)
def load_registry(path: Optional[str] = None) -> dict[str, ModelSpec]:
    """등록표 파일 → {모델: ModelSpec}. 파일 형식이 틀리면 LLMBackendError."""
    p = Path(path) if path else REGISTRY_PATH
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError as e:
        raise LLMBackendError(f"모델 등록표를 읽을 수 없다: {p} ({type(e).__name__})") from None
    models = doc.get("models") or {}
    if not isinstance(models, dict):
        raise LLMBackendError(f"모델 등록표 {p}: models가 표가 아니다")
    return {str(n): _spec_from(str(n), raw, source="registry", require_checked=True) for n, raw in models.items()}


def get_spec(model: str, *, registry_path: Optional[str] = None) -> ModelSpec:
    """등록된 모델의 명세. 프로세스 안 임시 등록이 파일보다 우선한다. 없으면 LLMBackendError."""
    if model in _runtime:
        return _runtime[model]
    reg = load_registry(registry_path)
    if model not in reg:
        raise LLMBackendError(f"등록되지 않은 모델이다: {model!r} — configs/llm_models.yaml에 없다. 새 모델은 "
                              "scripts/check_llm_model.py로 합성 확인한 뒤 등록한다 (docs/extraction.md)")
    return reg[model]


def get_profile(model_or_spec: str | ModelSpec) -> ModuleType:
    """모델(또는 명세)의 계열 모듈을 import해 돌려준다."""
    spec = model_or_spec if isinstance(model_or_spec, ModelSpec) else get_spec(model_or_spec)
    return importlib.import_module(f".{spec.profile}", __name__)


def register_model(name: str, profile: str, *, efforts: Optional[list[str]] = None, checked_at: Optional[str] = None,
                   note: str = "") -> ModelSpec:
    """프로세스 안에서만 임시 등록한다(테스트·합성 확인용). 등록표 파일은 바꾸지 않는다."""
    spec = _spec_from(name, {"profile": profile, "efforts": efforts, "checked_at": checked_at, "note": note},
                      source="runtime", require_checked=False)
    _runtime[name] = spec
    return spec


def unregister_model(name: str) -> None:
    _runtime.pop(name, None)
