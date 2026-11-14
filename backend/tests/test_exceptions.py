"""异常输入单测：非法参数必须显式抛错，不许静默返回脏值。

对应 TEST-PLAN §1「异常输入 ≥ 2」：阈值越界、负体积、非正半衰期、非法窗口长度、
负检索输入、非正恢复速度 / 恢复时限。
"""
from __future__ import annotations

from datetime import date

import pytest

from src.services import cost_core, heat_core, savings_core


@pytest.mark.parametrize(("value", "message"), [(-0.1, "0 到 100"), (100.1, "0 到 100"), (999, "0 到 100")])
def test_threshold_out_of_range_raises(value: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        savings_core.validate_threshold(value)


def test_threshold_non_numeric_raises() -> None:
    with pytest.raises(ValueError, match="threshold 必须是数字"):
        savings_core.validate_threshold("not-a-number")


def test_negative_size_raises_instead_of_dirty_saving() -> None:
    with pytest.raises(ValueError, match="size_gb 必须 >= 0"):
        cost_core.compare_tiers(-100, "hot", price_before=1.05, warm_price=0.85, cold_price=0.55)


def test_negative_retrieval_usage_raises() -> None:
    with pytest.raises(ValueError, match="必须 >= 0"):
        cost_core.monthly_retrieval(-1024, 0)
    with pytest.raises(ValueError, match="必须 >= 0"):
        cost_core.monthly_retrieval(0, -3)


def test_non_positive_restore_speed_raises() -> None:
    with pytest.raises(ValueError, match="恢复速度必须 > 0"):
        cost_core.restore_hours(4800, 0)


def test_non_positive_restore_deadline_raises() -> None:
    with pytest.raises(ValueError, match="恢复时限必须 > 0"):
        cost_core.compare_tiers(
            100, "hot", price_before=1.05, warm_price=0.85, cold_price=0.55, restore_deadline_h=0
        )


@pytest.mark.parametrize("half_life", [0.0, -14.0])
def test_non_positive_half_life_raises(half_life: float) -> None:
    with pytest.raises(ValueError, match="half_life 必须 > 0"):
        heat_core.decay_weight(0, half_life)


def test_negative_window_raises() -> None:
    with pytest.raises(ValueError, match="window_days 必须 >= 1"):
        heat_core.window_dates(date(2026, 11, 12), -1)


def test_usage_window_raises() -> None:
    with pytest.raises(ValueError, match="window_days 必须 >= 1"):
        heat_core.aggregate_usage([(3, 1024)], 0)
