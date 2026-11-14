"""算法 2（温冷对比 / 检索成本 / 恢复时限 / 风险闸门）纯函数单测。

期望值来源：batch-plan §5.6 的口径手算（热区 1.05 / 温区 0.85 / 冷区 0.55 ¥/GB·月，
迁移 0.75 ¥/GB + 270 ¥/表，检索 0.2 ¥/GB + 6 ¥/千次，恢复 500 GB/h、时限 8h）。
"""
from __future__ import annotations

import pytest

from src.services import cost_core

# seed 策略单价
HOT, WARM, COLD = 1.05, 0.85, 0.55
MIGRATE_PRICE, MIGRATE_FIXED = 0.75, 270.0
RETRIEVAL_GB, REQUEST_1K = 0.2, 6.0
RESTORE_SPEED, RESTORE_DEADLINE = 500.0, 8.0


def _compare(size_gb: float, from_tier: str = "hot", **overrides) -> dict:
    payload = {
        "price_before": {"hot": HOT, "warm": WARM, "cold": COLD}[from_tier],
        "warm_price": WARM,
        "cold_price": COLD,
        "retrieval_month_cold": 0.0,
        "restore_h": size_gb / RESTORE_SPEED,
        "restore_deadline_h": RESTORE_DEADLINE,
        "migrate_price": MIGRATE_PRICE,
        "migrate_fixed": MIGRATE_FIXED,
    }
    payload.update(overrides)
    return cost_core.compare_tiers(size_gb, from_tier, **payload)


# --------------------------------------------------------------- 检索成本 ----
def test_monthly_retrieval_hand_computed() -> None:
    """90 天扫 900 GiB + 9,000 次查询 -> 月 300 GB × 0.2 + 3 千次 × 6 = ¥78。"""
    cost = cost_core.monthly_retrieval(
        900 * 1024 ** 3, 9000, retrieval_price_gb=RETRIEVAL_GB, request_price_1k=REQUEST_1K
    )
    assert cost == pytest.approx(78.0, rel=1e-9)


def test_monthly_retrieval_zero_usage_is_free() -> None:
    assert cost_core.monthly_retrieval(0, 0) == 0.0


@pytest.mark.parametrize(("scan", "query"), [(-1, 0), (0, -5)])
def test_monthly_retrieval_rejects_negative(scan: float, query: float) -> None:
    with pytest.raises(ValueError, match="必须 >= 0"):
        cost_core.monthly_retrieval(scan, query)


# --------------------------------------------------------------- 恢复时限 ----
def test_restore_hours_hand_computed() -> None:
    """4800 GB / 500 GB·h = 9.6h；0 GB = 0h。"""
    assert cost_core.restore_hours(4800, RESTORE_SPEED) == pytest.approx(9.6)
    assert cost_core.restore_hours(0, RESTORE_SPEED) == 0.0


def test_restore_hours_rejects_non_positive_speed() -> None:
    with pytest.raises(ValueError, match="恢复速度必须 > 0"):
        cost_core.restore_hours(100, 0)


# ---------------------------------------------------------- 温冷对比（目标温区） ----
def test_compare_restore_timeout_downgrades_to_warm() -> None:
    """dwd.trade_order_detail：4800 GB 恢复 9.6h > 时限 8h -> 降级温区（即使冷区更省）。"""
    econ = _compare(4800, retrieval_month_cold=1926.77)
    assert econ["target_tier"] == "warm"
    assert econ["restore_ok"] is False
    assert econ["restore_hours"] == 9.6
    assert econ["restore_verdict"] == "冷区恢复约 9.6h > 时限 8h，恢复超时"
    assert "超过时限 8h，降级温区" in econ["target_reason"]
    # 温区方案：月省 4800×(1.05-0.85)=960，迁移 3870，回收期 4.0，净年省 7650
    assert econ["save_month"] == 960.0
    assert econ["retrieval_month"] == 0.0          # 温区不收检索费
    assert econ["migration_cost"] == 3870.0
    assert econ["payback_months"] == 4.0
    assert econ["net_save_year"] == 7650.0
    # 冷区方案仍然算出来供对照：月省 2400-1926.77=473.23
    assert econ["options"]["cold"]["save_month"] == 473.23


def test_compare_retrieval_eats_spread_and_stays_warm() -> None:
    """ods.click_stream：检索费 ¥555/月吃掉价差 -> 冷区净年省 1890 < 温区 2502，留温区。"""
    econ = _compare(1680, retrieval_month_cold=555.0)
    assert econ["target_tier"] == "warm"
    assert econ["restore_ok"] is True               # 3.4h 恢复达标，是检索费打回的
    assert "吃掉价差" in econ["target_reason"]
    assert econ["options"]["cold"]["save_month"] == 285.0
    assert econ["options"]["cold"]["net_save_year"] == 1890.0
    assert econ["save_month"] == 336.0
    assert econ["net_save_year"] == 2502.0


def test_compare_cold_wins_when_retrieval_affordable() -> None:
    """dwd.inventory_move_detail：检索费 ¥311.83/月 后冷区仍最优 -> 目标冷区。"""
    econ = _compare(1260, retrieval_month_cold=311.83)
    assert econ["target_tier"] == "cold"
    assert econ["save_month"] == 318.17             # 630 - 311.83
    assert econ["retrieval_month"] == 311.83
    assert econ["migration_cost"] == 1215.0         # 1260×0.75 + 270
    assert econ["payback_months"] == 3.8
    assert econ["net_save_year"] == 2603.04         # 318.17×12 - 1215
    assert econ["restore_verdict"] == "冷区恢复约 2.5h ≤ 时限 8h，满足报表时限"


def test_compare_zero_scan_table_cold_net_matches_legacy() -> None:
    """零扫描表（检索费 0）：冷区净年省与旧口径一致 —— 480 GB 热区 -> 冷区。"""
    econ = _compare(480)
    assert econ["target_tier"] == "cold"
    assert econ["save_month"] == 240.0
    assert econ["net_save_year"] == 2250.0          # 240×12 - 630


def test_compare_warm_table_noop_when_cold_not_worth_it() -> None:
    """温区表 + 高检索费：冷区净省为负 -> 留温区（不迁移：迁移成本 0、月省 0）。"""
    econ = _compare(4800, from_tier="warm", retrieval_month_cold=2000.0)
    assert econ["target_tier"] == "warm"
    assert econ["save_month"] == 0.0
    assert econ["migration_cost"] == 0.0            # 留在温区不发生迁移
    assert econ["payback_months"] is None
    assert econ["net_save_year"] == 0.0


def test_compare_warm_table_moves_cold_when_profitable() -> None:
    """温区表 + 零检索：冷区净年省 4,518 > 温区 0 -> 迁冷（1680 GB 恢复 3.4h 达标）。"""
    econ = _compare(1680, from_tier="warm")
    assert econ["target_tier"] == "cold"
    assert econ["save_month"] == 504.0              # 1680×(0.85-0.55)
    assert econ["migration_cost"] == 1530.0
    assert econ["net_save_year"] == 4518.0


def test_compare_cold_table_stays_put() -> None:
    """已在冷区：保持归档，不发生迁移。"""
    econ = _compare(900, from_tier="cold")
    assert econ["target_tier"] == "cold"
    assert econ["save_month"] == 0.0
    assert econ["migration_cost"] == 0.0
    assert econ["target_reason"] == "已在冷区，保持归档"


def test_compare_zero_size_degrades_to_noop() -> None:
    """0 GB：月省 0 -> 回收期 None；两方案净年省相同（-270 工单费），保守留温区。"""
    econ = _compare(0)
    assert econ["save_month"] == 0.0
    assert econ["payback_months"] is None
    assert econ["target_tier"] == "warm"            # 净年省持平，冷区无额外收益不冒险


def test_payback_months_none_when_no_saving() -> None:
    assert cost_core.payback_months(1000.0, 0.0) is None
    assert cost_core.payback_months(1000.0, -5.0) is None
    assert cost_core.payback_months(1000.0, 400.0) == 2.5


# ------------------------------------------------------- 风险闸门（分级） ----
def test_assess_risk_block_wins_over_warn() -> None:
    """近 7 天有访问 + 报表引用 + 单分区：block 优先级最高，五条检查全量返回。"""
    verdict = cost_core.assess_risk(
        recent_block_access=103,
        report_refs=2,
        partition_count=1,
        report_names=["促销效果周报", "渠道费用月报"],
        stat_days=90,
    )
    assert verdict["risk"] == "block"
    assert [check["name"] for check in verdict["checks"]] == [
        "recent_access",
        "stats_window",
        "report_refs",
        "partition_count",
        "restore_sla",
    ]
    assert [check["level"] for check in verdict["checks"]] == ["block", "ok", "warn", "warn", "ok"]
    assert "近 7 天仍有 103 次访问" in verdict["reasons"]
    assert "被 2 张报表引用（促销效果周报、渠道费用月报）" in verdict["reasons"]


def test_assess_risk_incomplete_stats_is_block() -> None:
    """迟到记录未补齐：近 90 天统计仅 40 天 -> block（缺少完整统计不得提交）。"""
    verdict = cost_core.assess_risk(
        recent_block_access=0, report_refs=0, partition_count=12, stat_days=40, window_days=90
    )
    assert verdict["risk"] == "block"
    assert verdict["checks"][1] == {"name": "stats_window", "level": "block", "value": 40}
    assert "近 90 天统计仅 40 天" in verdict["reasons"]
    assert "不得提交" in verdict["reasons"]


def test_assess_risk_restore_timeout_is_warn() -> None:
    """冷区恢复超时（已降级温区测算）-> warn，不阻断提交。"""
    verdict = cost_core.assess_risk(
        recent_block_access=0,
        report_refs=0,
        partition_count=36,
        stat_days=90,
        restore_ok=False,
        restore_hours=9.6,
        restore_deadline_h=8.0,
    )
    assert verdict["risk"] == "warn"
    assert verdict["checks"][4] == {"name": "restore_sla", "level": "warn", "value": 9.6}
    assert "冷区恢复约 9.6h 超过时限 8h，已降级按温区测算" in verdict["reasons"]


def test_assess_risk_report_refs_is_warn() -> None:
    verdict = cost_core.assess_risk(
        recent_block_access=0, report_refs=2, partition_count=12, stat_days=90
    )
    assert verdict["risk"] == "warn"
    assert "需报表 owner 确认" in verdict["reasons"]


def test_assess_risk_low_partition_is_warn() -> None:
    verdict = cost_core.assess_risk(
        recent_block_access=0, report_refs=0, partition_count=1, stat_days=90
    )
    assert verdict["risk"] == "warn"
    assert "仅 1 个分区（< 3），无法按分区灰度回迁" in verdict["reasons"]


def test_assess_risk_ok_reason_is_canonical() -> None:
    verdict = cost_core.assess_risk(
        recent_block_access=0, report_refs=0, partition_count=36, stat_days=90, restore_hours=1.7
    )
    assert verdict["risk"] == "ok"
    assert verdict["reasons"] == "统计完整、近 7 天零访问、无报表引用、分区数满足灰度要求、恢复时限达标"
    assert [check["level"] for check in verdict["checks"]] == ["ok"] * 5


def test_assess_risk_missing_stats_degrade_to_block() -> None:
    """统计天数字段缺失（None）：按 0 天处理 -> 统计不完整 block，绝不静默放过。"""
    verdict = cost_core.assess_risk(recent_block_access=None, report_refs=None, partition_count=None, stat_days=None)
    assert verdict["risk"] == "block"
    assert verdict["checks"][1] == {"name": "stats_window", "level": "block", "value": 0}


_RANK = {"ok": 0, "warn": 1, "block": 2}

# 异常程度递增的输入序列：期望风险等级按 ok <= warn <= block 单调不减
_ANOMALY_LADDER = [
    {"recent_block_access": 0, "report_refs": 0, "partition_count": 36, "stat_days": 90},
    {"recent_block_access": 0, "report_refs": 0, "partition_count": 36, "stat_days": 90, "restore_ok": False, "restore_hours": 9.6},
    {"recent_block_access": 0, "report_refs": 0, "partition_count": 2, "stat_days": 90},
    {"recent_block_access": 0, "report_refs": 2, "partition_count": 1, "stat_days": 90},
    {"recent_block_access": 5, "report_refs": 0, "partition_count": 36, "stat_days": 90},
    {"recent_block_access": 0, "report_refs": 0, "partition_count": 36, "stat_days": 40},
]


def test_assess_risk_level_monotonic_by_anomaly() -> None:
    ranks = [_RANK[cost_core.assess_risk(**case)["risk"]] for case in _ANOMALY_LADDER]
    assert ranks == [0, 1, 1, 1, 2, 2]
    assert all(later >= earlier for earlier, later in zip(ranks, ranks[1:]))


# ------------------------------------------------------------ 异常输入 ----
@pytest.mark.parametrize("size_gb", [-1, -4800.5])
def test_compare_rejects_negative_size(size_gb: float) -> None:
    with pytest.raises(ValueError, match="size_gb 必须 >= 0"):
        _compare(size_gb)


def test_compare_rejects_non_positive_deadline() -> None:
    with pytest.raises(ValueError, match="恢复时限必须 > 0"):
        _compare(100, restore_deadline_h=0)


@pytest.mark.parametrize(
    ("price_before", "price_after"),
    [(-1.0, 0.55), (1.05, -0.55)],
)
def test_tier_option_rejects_negative_prices(price_before: float, price_after: float) -> None:
    with pytest.raises(ValueError, match="必须 >= 0"):
        cost_core.tier_option(100, price_before, price_after, tier="cold")
