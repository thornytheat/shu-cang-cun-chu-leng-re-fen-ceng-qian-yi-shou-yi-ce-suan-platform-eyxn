"""聚合口径纯函数单测（试算 / 计划提交共用）。

覆盖：Σ 汇总（含检索费与目标温区计数）、加权回收期、风险计数守恒、顺序无关、
候选筛选与排序、阈值校验、统计完整性闸门。
期望值来自 batch-plan §5.6 主流程验收（12 张 / 6 张两套数字，温冷对比口径）。
"""
from __future__ import annotations

import pytest

from scripts import fixtures
from src.services import savings_core


def test_aggregate_empty_degrades_to_zero() -> None:
    summary = savings_core.aggregate([])
    assert summary == {
        "count": 0,
        "size_total_gb": 0.0,
        "save_month": 0.0,
        "save_year": 0.0,
        "retrieval_month": 0.0,
        "migration_cost": 0.0,
        "net_save_year": 0.0,
        "avg_payback": None,
        "risk_block_count": 0,
        "risk_warn_count": 0,
        "target_warm_count": 0,
        "target_cold_count": 0,
        "stats_incomplete_count": 0,
    }


def test_aggregate_trial_hits_matches_acceptance(trial_hits: list[dict]) -> None:
    """阈值 20 -> 12 张：体积 13,680 GB、月省 3,972.21（已扣检索费 923.79/月）、净年省 34,166.46。"""
    summary = savings_core.aggregate(trial_hits)
    assert summary["count"] == 12
    assert summary["size_total_gb"] == 13680.0
    assert summary["save_month"] == 3972.21
    assert summary["save_year"] == 47666.52
    assert summary["retrieval_month"] == 923.79
    assert summary["migration_cost"] == 13500.0        # 0.75×13680 + 270×12
    assert summary["net_save_year"] == 34166.46        # 47666.52 - 13500
    assert summary["avg_payback"] == 3.4               # 13500/3972.21 = 3.3986
    assert (summary["risk_block_count"], summary["risk_warn_count"]) == (2, 3)
    assert (summary["target_warm_count"], summary["target_cold_count"]) == (2, 10)
    assert summary["stats_incomplete_count"] == 0


def test_aggregate_plan_after_blockers_removed_matches_acceptance(trial_hits: list[dict]) -> None:
    """主流程：前 8 张去掉 2 张 block -> 6 张，净年省 21,027.07、平均回收期 3.6 月。"""
    top8 = trial_hits[:8]
    remaining = [item for item in top8 if item["risk"] != "block"]
    summary = savings_core.aggregate(remaining)
    assert summary["count"] == 6
    assert summary["size_total_gb"] == 10020.0
    assert summary["save_month"] == 2513.51
    assert summary["save_year"] == 30162.12
    assert summary["retrieval_month"] == 552.49
    assert summary["migration_cost"] == 9135.0
    assert summary["net_save_year"] == 21027.07
    assert summary["avg_payback"] == 3.6               # 9135/2513.51 = 3.6344
    assert summary["risk_block_count"] == 0
    assert (summary["target_warm_count"], summary["target_cold_count"]) == (2, 4)


def test_aggregate_is_order_independent(trial_hits: list[dict]) -> None:
    forward = savings_core.aggregate(trial_hits)
    backward = savings_core.aggregate(list(reversed(trial_hits)))
    assert forward == backward


def test_aggregate_count_conservation(candidate_rows: list[dict]) -> None:
    """计数守恒：block + warn + ok == 总行数；目标温区 温 + 冷 == 总行数。"""
    summary = savings_core.aggregate(candidate_rows)
    ok = sum(1 for item in candidate_rows if item["risk"] == "ok")
    assert summary["count"] == 42
    assert summary["risk_block_count"] + summary["risk_warn_count"] + ok == 42
    assert summary["target_warm_count"] + summary["target_cold_count"] == 42


def test_aggregate_summary_equals_sum_of_rows(trial_hits: list[dict]) -> None:
    """汇总与逐表结果一致：Σ逐表月省 == 汇总月省（分毫不差）。"""
    summary = savings_core.aggregate(trial_hits)
    assert summary["save_month"] == pytest.approx(sum(item["save_month"] for item in trial_hits))
    assert summary["retrieval_month"] == pytest.approx(sum(item["retrieval_month"] for item in trial_hits))
    assert summary["net_save_year"] == pytest.approx(sum(item["net_save_year"] for item in trial_hits))


def test_select_candidates_matches_primary_flow(trial_hits: list[dict]) -> None:
    """试算前 8 行必须就是主流程选中的 8 张表（排序键 = 月省倒序）。"""
    assert [item["full_name"] for item in trial_hits[:8]] == list(fixtures.PRIMARY_FLOW_SELECTION)


def test_select_candidates_excludes_cold_and_respects_limit(candidate_rows: list[dict]) -> None:
    hits = savings_core.select_candidates(candidate_rows, 20.0, max_rows=5)
    assert len(hits) == 5
    assert all(item["tier"] != "cold" for item in hits)


def test_select_candidates_threshold_is_monotonic(candidate_rows: list[dict]) -> None:
    """阈值越高命中越多（更宽）：0 -> 20 -> 30 -> 60 -> 100 数量单调不减。"""
    counts = [
        len(savings_core.select_candidates(candidate_rows, threshold))
        for threshold in (0, 20, 30, 60, 100)
    ]
    assert counts[0] == 0          # 热度分不可能 < 0
    assert counts[1] == 12         # 验收口径：阈值 20 命中 12 张
    assert all(later >= earlier for earlier, later in zip(counts, counts[1:]))


def test_blockers_preserve_order(trial_hits: list[dict]) -> None:
    names = savings_core.blockers(trial_hits[:8])
    assert names == ["ads.monthly_finance_report_base", "dws.store_daily_gmv"]
    assert savings_core.blockers([]) == []


def test_incomplete_stats_gate(candidate_rows: list[dict]) -> None:
    """缺少完整统计的表（40/40/10 天）被逐一点名；完整表不在名单里。"""
    names = savings_core.incomplete_stats(candidate_rows)
    assert set(names) == {
        "dws.promo_effect_weekly",
        "ads.marketing_roi_monthly",
        "ads.realtime_gmv_cache",
    }
    assert savings_core.incomplete_stats([]) == []
    # 主流程 12 张候选统计全部完整 -> 可以提交
    assert savings_core.incomplete_stats(savings_core.select_candidates(candidate_rows, 20.0)) == []


@pytest.mark.parametrize("value", [0, 0.0, 20, 100, 100.0, "20"])
def test_validate_threshold_accepts_bounds(value) -> None:
    assert 0.0 <= savings_core.validate_threshold(value) <= 100.0


@pytest.mark.parametrize("value", [-0.1, -20, 100.1, 1000, "abc", None])
def test_validate_threshold_rejects_out_of_range(value) -> None:
    with pytest.raises(ValueError):
        savings_core.validate_threshold(value)


def test_check_plan_size_rejects_over_limit() -> None:
    savings_core.check_plan_size([str(index) for index in range(60)])   # 60 张允许
    with pytest.raises(ValueError, match="单次最多 60 张表"):
        savings_core.check_plan_size([str(index) for index in range(61)])
