"""试算：按阈值粗筛"降冷候选"，逐表做温冷对比测算并给出风险闸门结论。

口径说明（以 TrialResultOut 回传为准）：
  候选 = 当前不在冷区 且 热度分 < threshold 的表（阈值粗筛，允许误伤）
  目标温区 = 温区方案 vs 冷区方案取首年净省更高者；冷区恢复超时 -> 降级温区
  检索成本 = 月扫描GB × 检索单价 + 月查询次数/1000 × 请求单价（仅冷区；近 90 天统计折算，迟到记录不按零查询）
  月省 = size × (当前层单价 - 目标层单价) - 检索成本；年省 = 月省 × 12
  净年省 = 年省 - (size × 迁移单价 + 每表固定工单成本)
  风险 = 算法 2 的闸门（近 7 天访问 block / 统计不完整 block / 报表引用 warn / 分区 <3 warn / 恢复超时 warn）
  平均回收期 = 总迁移成本 / 总月省（按节省额加权，不是逐表算术平均）
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.enums import StorageTier
from src.constants.log_templates import ACTION_TRIAL_RUN
from src.constants.limits import MAX_TRIAL_ROWS
from src.models.table_profile import TableProfile
from src.models.user import User
from src.services import audit_service, cost_service, heat_service, savings_core
from src.utils.errors import AppError

GIB = 1024 ** 3


def build_hits(db: Session, group, threshold: float) -> list[dict]:
    """逐表构建候选行，再交给纯函数层做「非冷区 + 热度 < 阈值」筛选与排序。"""
    scores = heat_service.compute_scores(db, group)
    profiles = db.execute(select(TableProfile)).scalars().all()
    rows: list[dict] = []
    for profile in profiles:
        hot = scores.get(str(profile.id))
        if hot is None:
            continue
        econ = cost_service.economics(float(profile.size_gb), profile.tier, group, hot)
        verdict = cost_service.assess_risk(profile, hot, group, econ)
        rows.append(
            {
                "table_id": str(profile.id),
                "full_name": profile.full_name,
                "layer": profile.layer,
                "tier": profile.tier,
                "size_gb": float(profile.size_gb),
                "partition_count": int(profile.partition_count),
                "report_refs": int(profile.report_refs or 0),
                "hot_score": hot.hot_score,
                "suggested_tier": hot.suggested_tier,
                "target_tier": econ.target_tier,
                "save_month": econ.save_month,
                "retrieval_month": econ.retrieval_month,
                "migration_cost": econ.migration_cost,
                "payback_months": econ.payback_months,
                "net_save_year": econ.net_save_year,
                "restore_hours": econ.restore_hours,
                "restore_ok": econ.restore_ok,
                "restore_verdict": econ.restore_verdict,
                "query_90": round(hot.query_90),
                "scan_gb_90": round(hot.scan_bytes_90 / GIB, 1),
                "stats_days": hot.stat_days,
                "stats_complete": hot.stats_complete,
                "risk": verdict.risk,
                "reasons": verdict.reasons,
                "recent_7_access": hot.recent_7_access,
                "recent_30_access": hot.recent_30_access,
            }
        )

    return savings_core.select_candidates(rows, threshold, max_rows=MAX_TRIAL_ROWS)


def aggregate(hits: list[dict]) -> dict:
    """试算汇总（口径与计划提交共用 savings_core.aggregate，汇总 = 逐表之和）。"""
    summary = savings_core.aggregate(hits)
    return {
        "hit_count": summary["count"],
        "size_total_gb": summary["size_total_gb"],
        "save_month": summary["save_month"],
        "save_year": summary["save_year"],
        "retrieval_month": summary["retrieval_month"],
        "migration_cost": summary["migration_cost"],
        "net_save_year": summary["net_save_year"],
        "avg_payback": summary["avg_payback"],
        "risk_block_count": summary["risk_block_count"],
        "risk_warn_count": summary["risk_warn_count"],
        "target_warm_count": summary["target_warm_count"],
        "target_cold_count": summary["target_cold_count"],
        "stats_incomplete_count": summary["stats_incomplete_count"],
    }


def run_trial(db: Session, group, threshold: float, actor: User | None = None, trace_id: str = "") -> dict:
    try:
        threshold = savings_core.validate_threshold(threshold)
    except ValueError as exc:
        raise AppError(codes.THRESHOLD_INVALID, "阈值必须落在 0 到 100 之间", f"threshold={threshold}") from exc
    hits = build_hits(db, group, threshold)
    summary = aggregate(hits)
    evaluated = db.query(TableProfile).filter(TableProfile.tier != StorageTier.COLD).count()

    if actor is not None:
        audit_service.record(
            db,
            action=ACTION_TRIAL_RUN,
            actor=actor,
            entity_type="Trial",
            entity_id=f"th{threshold}",
            detail={
                "threshold": threshold,
                "hit_count": summary["hit_count"],
                "save_month": summary["save_month"],
                "retrieval_month": summary["retrieval_month"],
                "block": summary["risk_block_count"],
                "warn": summary["risk_warn_count"],
            },
            trace_id=trace_id,
        )

    return {
        "policy_version": group.version,
        "threshold": threshold,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "evaluated_count": int(evaluated),
        **summary,
        "hits": hits,
    }
