"""유사도 가중치 설정. 문서: docs/spec.md Step 9, §7.3. 기본값 파일: configs/weights.yaml"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "configs" / "weights.yaml"
SCORED = ("color", "detail", "pattern", "material", "fit")


class CategoryConfig(BaseModel):
    major: Literal["gate"] = "gate"  # 상분류는 후보 조건. 점수에 쓰지 않는다
    minor_bonus_alpha: float = Field(0.1, ge=0)


class FocusMultipliers(BaseModel):
    core: float = Field(2.0, gt=0)
    supporting: float = Field(1.5, gt=0)
    default: float = Field(1.0, gt=0)


class WeightsConfig(BaseModel):
    base_weights: dict[str, float]
    category: CategoryConfig = Field(default_factory=CategoryConfig)
    focus_multipliers: FocusMultipliers = Field(default_factory=FocusMultipliers)
    _sha: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def _check_attributes(self) -> "WeightsConfig":
        if set(self.base_weights) != set(SCORED):
            raise ValueError(f"base_weights must have exactly {SCORED}")
        if any(w <= 0 for w in self.base_weights.values()):
            raise ValueError("base weights must be positive")
        return self

    @property
    def sha(self) -> str:
        """읽은 weights.yaml 파일의 sha256 앞 12자 (재현용 weights_sha). 파일 없이 만들면 내용 해시."""
        if self._sha:
            return self._sha
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()[:12]

    def _multiplier(self, attribute: str, focus_core: Iterable[str], focus_supporting: Iterable[str]) -> float:
        m = self.focus_multipliers
        if attribute in set(focus_core):
            return m.core
        if attribute in set(focus_supporting):
            return m.supporting
        return m.default

    def applied_weight(
        self, attribute: str, focus_core: Iterable[str] = (), focus_supporting: Iterable[str] = ()
    ) -> float:
        """적용 가중치 = 기본 가중치 × 트렌드 배수."""
        return self.base_weights[attribute] * self._multiplier(attribute, focus_core, focus_supporting)

    def applied_alpha(self, focus_core: Iterable[str] = (), focus_supporting: Iterable[str] = ()) -> float:
        """하분류 일치 보너스 = α × 트렌드 배수. 트렌드의 핵심·보조 속성에 `category`가 있을 때만 배수가 붙는다."""
        return self.category.minor_bonus_alpha * self._multiplier("category", focus_core, focus_supporting)


def load_weights(path: Path | str = DEFAULT_PATH) -> WeightsConfig:
    raw = Path(path).read_bytes()
    cfg = WeightsConfig.model_validate(yaml.safe_load(raw.decode("utf-8")))
    cfg._sha = hashlib.sha256(raw).hexdigest()[:12]
    return cfg
