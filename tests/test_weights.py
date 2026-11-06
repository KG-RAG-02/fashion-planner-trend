import pytest
from pydantic import ValidationError

from trend_agent.retrieval.weights import WeightsConfig, load_weights


def test_default_weights_match_decision():
    w = load_weights()
    assert w.base_weights == {"color": 3, "detail": 3, "pattern": 2, "material": 2, "fit": 2}
    assert w.category.major == "gate"            # 상분류는 점수가 아니라 후보 조건
    assert "category" not in w.base_weights      # 카테고리는 가중치 속성이 아니다
    assert w.category.minor_bonus_alpha == 0.1   # 하분류는 보너스
    assert (w.focus_multipliers.core, w.focus_multipliers.supporting, w.focus_multipliers.default) == (2.0, 1.5, 1.0)


def test_applied_weight_uses_trend_focus():
    w = load_weights()
    assert w.applied_weight("pattern", ["pattern"], ["detail"]) == 4.0   # 2 × 2.0
    assert w.applied_weight("detail", ["pattern"], ["detail"]) == 4.5    # 3 × 1.5
    assert w.applied_weight("color", ["pattern"], ["detail"]) == 3.0     # 해당 없음
    assert w.applied_weight("color") == 3.0                              # 트렌드 정보가 없으면 기본값


def test_category_focus_scales_minor_bonus_only():
    w = load_weights()
    assert w.applied_alpha() == 0.1                          # category가 없으면 α 그대로
    assert w.applied_alpha(["category"]) == 0.2              # core → α × 2.0
    assert w.applied_alpha([], ["category"]) == pytest.approx(0.15)
    assert w.applied_alpha(["pattern"]) == 0.1               # 다른 속성의 핵심 지정은 α에 영향 없음
    assert w.applied_weight("color", ["category"]) == 3.0    # category 핵심은 속성 가중치에 영향 없음


def test_multipliers_of_one_use_base_weights_only():
    w = load_weights()
    w.focus_multipliers.core = w.focus_multipliers.supporting = 1.0
    assert w.applied_weight("pattern", ["pattern"]) == 2.0


def test_invalid_config_rejected():
    with pytest.raises(ValidationError):
        WeightsConfig(base_weights={"color": 3})
    with pytest.raises(ValidationError):
        WeightsConfig(base_weights={"color": 3, "detail": 3, "pattern": 2, "material": 2, "fit": 0})
