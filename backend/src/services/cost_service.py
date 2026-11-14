"""算法 2（收益部分）：温冷对比、检索成本、恢复时限与风险闸门。

口径与实现分层：
  - 纯函数在 `cost_core.py`（被 pytest 直接单测，不碰 DB）；
  - 本模块负责从 TierPolicy 策略组 / ORM 档案取值，再委托 core，返回结构化结果。

  检索成本    retrieval_month = 月扫描GB × 检索单价 + 月查询次数/1000 × 请求单价（仅冷区）
  恢复时限    restore_hours = size_GB / 恢复速度；超时则冷区方案出局 -> 降级温区
  目标温区    温区方案 vs 冷区方案，取首年净省更高者（冷区已扣检索费）
  save_month  = size × (price_before − price_after) − retrieval_month
  migration_cost = size × migrate_price + migrate_fixed（目标 = 当前温区时为 0）
  net_save_year  = save_month × 12 − migration_cost

风险闸门（优先级 block > warn > ok）：
  近 block_recent_days(7) 天有访问          -> block（违反冷存前置条件，必须打回）
  统计天数 < 90 天窗口（迟到记录未补齐）     -> block（缺少完整统计不得提交）
  被报表引用 report_refs > 0                -> warn（需报表 owner 书面确认）
  分区数 < min_partition(3)                 -> warn（无法按分区灰度、回迁成本高）
  冷区恢复超时（已降级温区测算）             -> warn
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config.settings import settings
from src.constants.enums import StorageTier
from src.services import cost_core

WINDOW_DAYS = settings.heat_window_days


@dataclass
class Economics:
    target_tier: str
    target_reason: str
    price_before: float
    price_after: float
    cost_before: float
    cost_after: float
    retrieval_month: float
    save_month: float
    save_year: float
    migration_cost: float
    payback_months: float | None
    net_save_year: float
    restore_hours: float
    restore_deadline_h: float
    restore_ok: bool
    restore_verdict: str
    options: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "target_tier": self.target_tier,
            "target_reason": self.target_reason,
            "price_before": self.price_before,
            "price_after": self.price_after,
            "cost_before": self.cost_before,
            "cost_after": self.cost_after,
            "retrieval_month": self.retrieval_month,
            "save_month": self.save_month,
            "save_year": self.save_year,
            "migration_cost": self.migration_cost,
            "payback_months": self.payback_months,
            "net_save_year": self.net_save_year,
            "restore_hours": self.restore_hours,
            "restore_deadline_h": self.restore_deadline_h,
            "restore_ok": self.restore_ok,
            "restore_verdict": self.restore_verdict,
            "options": self.options,
        }


@dataclass
class RiskVerdict:
    risk: str
    reasons: str
    checks: list[dict]


def economics(size_gb: float, from_tier: str, group, hot) -> Economics:
    """单表温冷对比测算：检索费按近 90 天查询次数与扫描量（迟到记录不按零查询）。"""
    payload = cost_core.compare_tiers(
        size_gb,
        from_tier,
        price_before=group.price(from_tier),
        warm_price=group.price(StorageTier.WARM),
        cold_price=group.price(StorageTier.COLD),
        retrieval_month_cold=cost_core.monthly_retrieval(
            getattr(hot, "scan_bytes_90", 0.0),
            getattr(hot, "query_90", 0.0),
            retrieval_price_gb=group.retrieval_price_gb,
            request_price_1k=group.request_price_1k,
            window_days=WINDOW_DAYS,
        ),
        restore_h=cost_core.restore_hours(size_gb, group.restore_speed_gb_h),
        restore_deadline_h=group.restore_deadline_h,
        migrate_price=group.migrate_price,
        migrate_fixed=group.migrate_fixed,
    )
    return Economics(**payload)


def assess_risk(profile, hot, group, econ: Economics) -> RiskVerdict:
    payload = cost_core.assess_risk(
        recent_block_access=getattr(hot, "recent_7_access", 0),
        report_refs=getattr(profile, "report_refs", 0),
        partition_count=getattr(profile, "partition_count", 0),
        report_names=getattr(profile, "report_names", None),
        stat_days=getattr(hot, "stat_days", 0),
        window_days=WINDOW_DAYS,
        restore_ok=econ.restore_ok,
        restore_hours=econ.restore_hours,
        restore_deadline_h=econ.restore_deadline_h,
        block_recent_days=group.block_recent_days,
        min_partition=group.min_partition,
        quiet_days=group.quiet_days,
    )
    return RiskVerdict(payload["risk"], payload["reasons"], payload["checks"])
