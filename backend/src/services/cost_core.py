"""算法 2（温冷对比测算 + 检索成本 + 恢复时限 + 风险闸门）纯函数层。

只吃数值与「档案事实 dict」，**不 import Session / 不查库**，可被 pytest 直接单测。
`cost_service.py` 负责从 TierPolicy 策略组 / ORM 档案取值后调用这里。

口径（与 README「算法 2」一致）：

  检索成本    retrieval_month = 月扫描GB × 检索单价 + 月查询次数/1000 × 请求单价   # 仅冷区收取
             （月扫描GB / 月查询次数由近 90 天统计折算；迟到记录不按零查询，见 heat_core.aggregate_usage）
  恢复时限    restore_hours = size_GB / 恢复速度；超过 restore_deadline_h 冷区方案出局 -> 降级温区
  方案对比    每张表同时算「温区方案」与「冷区方案」，目标温区取首年净省更高者
  save_month  = size × (price_before − price_after) − retrieval_month
  migration_cost = size × migrate_price + migrate_fixed（目标温区 = 当前温区时为 0，不迁移）
  payback_months = migration_cost / save_month          （save_month ≤ 0 -> None）
  net_save_year  = save_month × 12 − migration_cost

风险闸门优先级 block > warn > ok：
  近 block_recent_days 天有访问            -> block
  统计天数 < window_days（迟到记录未补齐）  -> block（缺少完整统计不得提交）
  report_refs > 0                          -> warn（需报表 owner 确认）
  partition_count < min_partition          -> warn（无法按分区灰度回迁）
  冷区恢复超时（已降级温区测算）            -> warn
"""
from __future__ import annotations

from src.constants.enums import RiskLevel, StorageTier
from src.utils.numeric import money, ratio1

DEFAULT_MIGRATE_PRICE = 0.75
DEFAULT_MIGRATE_FIXED = 270.0
DEFAULT_BLOCK_RECENT_DAYS = 7
DEFAULT_MIN_PARTITION = 3
DEFAULT_QUIET_DAYS = 30
DEFAULT_WINDOW_DAYS = 90
# 冷区检索与恢复口径（策略组冷区行携带，可随版本调整）
DEFAULT_RETRIEVAL_PRICE_GB = 0.2      # 冷区检索单价 ¥/GB（按取回扫描量）
DEFAULT_REQUEST_PRICE_1K = 6.0        # 冷区请求单价 ¥/千次
DEFAULT_RESTORE_SPEED_GB_H = 500.0    # 冷区恢复速度 GB/小时
DEFAULT_RESTORE_DEADLINE_H = 8.0      # 恢复时限（小时，报表跑数窗口）

GIB = 1024 ** 3


def payback_months(migration_cost: float, save_month: float) -> float | None:
    """回收期（月，1 位小数）；月省 ≤ 0 时无意义 -> None（明确降级，不返回 inf/负值）。"""
    if save_month is None or float(save_month) <= 0:
        return None
    return ratio1(float(migration_cost) / float(save_month))


def monthly_retrieval(
    scan_bytes_90: float,
    query_count_90: float,
    *,
    retrieval_price_gb: float = DEFAULT_RETRIEVAL_PRICE_GB,
    request_price_1k: float = DEFAULT_REQUEST_PRICE_1K,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> float:
    """冷区月检索成本 = 月扫描量 × 检索单价 + 月查询次数 / 1000 × 请求单价。

    输入为近 window_days 天的查询次数与扫描字节（缺失天已按有记录天均值外推，
    不按零查询）；负输入没有业务含义 -> ValueError。
    """
    if float(scan_bytes_90) < 0 or float(query_count_90) < 0:
        raise ValueError(f"扫描量 / 查询次数必须 >= 0，收到 {scan_bytes_90} / {query_count_90}")
    if retrieval_price_gb < 0 or request_price_1k < 0:
        raise ValueError("检索 / 请求单价必须 >= 0")
    if window_days < 1:
        raise ValueError(f"window_days 必须 >= 1，收到 {window_days}")
    scan_gb_month = float(scan_bytes_90) / GIB * (30.0 / window_days)
    query_month = float(query_count_90) * (30.0 / window_days)
    return scan_gb_month * float(retrieval_price_gb) + (query_month / 1000.0) * float(request_price_1k)


def restore_hours(size_gb: float, speed_gb_h: float = DEFAULT_RESTORE_SPEED_GB_H) -> float:
    """冷区整表恢复耗时（小时）。非正恢复速度没有业务含义 -> ValueError。"""
    if float(size_gb) < 0:
        raise ValueError(f"size_gb 必须 >= 0，收到 {size_gb}")
    if float(speed_gb_h) <= 0:
        raise ValueError(f"恢复速度必须 > 0，收到 {speed_gb_h}")
    return float(size_gb) / float(speed_gb_h)


def tier_option(
    size_gb: float,
    price_before: float,
    price_after: float,
    *,
    tier: str,
    retrieval_month: float = 0.0,
    migrate_price: float = DEFAULT_MIGRATE_PRICE,
    migrate_fixed: float = DEFAULT_MIGRATE_FIXED,
    migrate: bool = True,
) -> dict:
    """单目标温区方案。migrate=False 表示「留在当前温区」：无迁移成本、无检索费变化。"""
    size = float(size_gb)
    if size < 0:
        raise ValueError(f"size_gb 必须 >= 0，收到 {size_gb}")
    if price_before < 0 or price_after < 0 or migrate_price < 0 or migrate_fixed < 0:
        raise ValueError("单价 / 迁移成本必须 >= 0")
    if retrieval_month < 0:
        raise ValueError(f"检索成本必须 >= 0，收到 {retrieval_month}")

    cost_before = size * float(price_before)
    cost_after = size * float(price_after)
    save_month = cost_before - cost_after - float(retrieval_month)
    migration_cost = (size * float(migrate_price) + float(migrate_fixed)) if migrate else 0.0
    return {
        "tier": tier,
        "price_after": money(price_after),
        "cost_after": money(cost_after),
        "retrieval_month": money(retrieval_month),
        "save_month": money(save_month),
        "save_year": money(save_month * 12),
        "migration_cost": money(migration_cost),
        "payback_months": payback_months(migration_cost, save_month),
        "net_save_year": money(save_month * 12 - migration_cost),
    }


def compare_tiers(
    size_gb: float,
    from_tier: str,
    *,
    price_before: float,
    warm_price: float,
    cold_price: float,
    retrieval_month_cold: float = 0.0,
    restore_h: float = 0.0,
    restore_deadline_h: float = DEFAULT_RESTORE_DEADLINE_H,
    migrate_price: float = DEFAULT_MIGRATE_PRICE,
    migrate_fixed: float = DEFAULT_MIGRATE_FIXED,
) -> dict:
    """温区 vs 冷区方案对比，选出目标温区并返回完整测算口径。

    目标温区决策：
      1. 冷区恢复超时（restore_h > restore_deadline_h）-> 降级温区（大表恢复时间不满足报表时限）；
      2. 否则取首年净省更高者（冷区净省已扣检索费，扫描多的表会被打回温区）；
      3. 当前已在冷区 -> 保持冷区（不再回摆）。
    """
    size = float(size_gb)
    if size < 0:
        raise ValueError(f"size_gb 必须 >= 0，收到 {size_gb}")
    if restore_deadline_h <= 0:
        raise ValueError(f"恢复时限必须 > 0，收到 {restore_deadline_h}")

    warm_opt = tier_option(
        size,
        price_before,
        warm_price,
        tier=StorageTier.WARM,
        retrieval_month=0.0,
        migrate_price=migrate_price,
        migrate_fixed=migrate_fixed,
        migrate=from_tier != StorageTier.WARM,
    )
    cold_opt = tier_option(
        size,
        price_before,
        cold_price,
        tier=StorageTier.COLD,
        retrieval_month=retrieval_month_cold,
        migrate_price=migrate_price,
        migrate_fixed=migrate_fixed,
        migrate=from_tier != StorageTier.COLD,
    )

    restore_ok = float(restore_h) <= float(restore_deadline_h)
    restore_h_1 = ratio1(restore_h) or 0.0
    if restore_ok:
        restore_verdict = f"冷区恢复约 {restore_h_1}h ≤ 时限 {restore_deadline_h:g}h，满足报表时限"
    else:
        restore_verdict = f"冷区恢复约 {restore_h_1}h > 时限 {restore_deadline_h:g}h，恢复超时"

    cold_net_raw = cold_opt["net_save_year"]
    warm_net_raw = warm_opt["net_save_year"]
    if from_tier == StorageTier.COLD:
        target_tier = StorageTier.COLD
        target_reason = "已在冷区，保持归档"
    elif not restore_ok:
        target_tier = StorageTier.WARM
        target_reason = f"冷区恢复约 {restore_h_1}h 超过时限 {restore_deadline_h:g}h，降级温区"
    elif cold_net_raw > warm_net_raw:
        target_tier = StorageTier.COLD
        target_reason = (
            f"冷区净年省 ¥{money(cold_net_raw)} 高于温区 ¥{money(warm_net_raw)}"
            f"（已扣检索费 ¥{money(retrieval_month_cold)}/月）"
        )
    else:
        target_tier = StorageTier.WARM
        target_reason = (
            f"冷区检索费 ¥{money(retrieval_month_cold)}/月 吃掉价差："
            f"净年省 ¥{money(cold_net_raw)} 不及温区 ¥{money(warm_net_raw)}，留温区"
        )

    chosen = cold_opt if target_tier == StorageTier.COLD else warm_opt
    return {
        "target_tier": target_tier,
        "target_reason": target_reason,
        "price_before": money(price_before),
        "price_after": chosen["price_after"],
        "cost_before": money(size * float(price_before)),
        "cost_after": chosen["cost_after"],
        "retrieval_month": chosen["retrieval_month"],
        "save_month": chosen["save_month"],
        "save_year": chosen["save_year"],
        "migration_cost": chosen["migration_cost"],
        "payback_months": chosen["payback_months"],
        "net_save_year": chosen["net_save_year"],
        "restore_hours": restore_h_1,
        "restore_deadline_h": float(restore_deadline_h),
        "restore_ok": restore_ok,
        "restore_verdict": restore_verdict,
        "options": {StorageTier.WARM: warm_opt, StorageTier.COLD: cold_opt},
    }


def assess_risk(
    *,
    recent_block_access: int | None,
    report_refs: int | None,
    partition_count: int | None,
    report_names: list[str] | None = None,
    stat_days: int | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    restore_ok: bool = True,
    restore_hours: float | None = None,
    restore_deadline_h: float = DEFAULT_RESTORE_DEADLINE_H,
    block_recent_days: int = DEFAULT_BLOCK_RECENT_DAYS,
    min_partition: int = DEFAULT_MIN_PARTITION,
    quiet_days: int = DEFAULT_QUIET_DAYS,
) -> dict:
    """风险闸门。缺失字段（None）按 0 处理并降级，而不是崩溃或静默放过：
    统计天数不足窗口 -> block（迟到记录未补齐，缺少完整统计不得提交）。"""
    checks: list[dict] = []
    blocked = False
    warned = False
    reasons: list[str] = []

    recent = int(recent_block_access or 0)
    if recent > 0:
        blocked = True
        checks.append({"name": "recent_access", "level": RiskLevel.BLOCK, "value": recent})
        reasons.append(
            f"近 {block_recent_days} 天仍有 {recent} 次访问（冷存要求近 {quiet_days} 天零访问）"
        )
    else:
        checks.append({"name": "recent_access", "level": RiskLevel.OK, "value": 0})

    days = int(stat_days or 0)
    if days < window_days:
        blocked = True
        checks.append({"name": "stats_window", "level": RiskLevel.BLOCK, "value": days})
        reasons.append(
            f"近 {window_days} 天统计仅 {days} 天，迟到记录未补齐，缺少完整统计不得提交"
        )
    else:
        checks.append({"name": "stats_window", "level": RiskLevel.OK, "value": days})

    refs = int(report_refs or 0)
    if refs > 0:
        warned = True
        names = "、".join(report_names or []) or f"{refs} 张报表"
        checks.append({"name": "report_refs", "level": RiskLevel.WARN, "value": refs})
        reasons.append(f"被 {refs} 张报表引用（{names}），需报表 owner 确认")
    else:
        checks.append({"name": "report_refs", "level": RiskLevel.OK, "value": 0})

    partitions = int(partition_count or 0)
    if partitions < min_partition:
        warned = True
        checks.append({"name": "partition_count", "level": RiskLevel.WARN, "value": partitions})
        reasons.append(f"仅 {partitions} 个分区（< {min_partition}），无法按分区灰度回迁")
    else:
        checks.append({"name": "partition_count", "level": RiskLevel.OK, "value": partitions})

    hours = ratio1(restore_hours) or 0.0
    if not restore_ok:
        warned = True
        checks.append({"name": "restore_sla", "level": RiskLevel.WARN, "value": hours})
        reasons.append(
            f"冷区恢复约 {hours}h 超过时限 {restore_deadline_h:g}h，已降级按温区测算"
        )
    else:
        checks.append({"name": "restore_sla", "level": RiskLevel.OK, "value": hours})

    if blocked:
        risk = RiskLevel.BLOCK
    elif warned:
        risk = RiskLevel.WARN
    else:
        risk = RiskLevel.OK
        reasons = ["统计完整、近 7 天零访问、无报表引用、分区数满足灰度要求、恢复时限达标"]
    return {"risk": risk, "reasons": "；".join(reasons), "checks": checks}
