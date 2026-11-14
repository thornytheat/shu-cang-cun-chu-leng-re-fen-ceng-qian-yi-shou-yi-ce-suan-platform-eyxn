"""温冷对比测算：检索费 / 恢复时限 / 目标温区 / 统计完整性

- tier_policies: + retrieval_price_gb / request_price_1k / restore_speed_gb_h / restore_deadline_h
- migration_plans: + retrieval_month
- migration_plan_items: + target_tier / retrieval_month / restore_hours / restore_ok
                        / restore_verdict / stats_days / stats_complete

Revision ID: 0002_tier_compare
Revises: 0001_initial
Create Date: 2026-11-14
"""
from __future__ import annotations

from alembic import op

from src.config.settings import settings

revision = "0002_tier_compare"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

SCHEMA = settings.db_schema

# (table, column, ddl)
COLUMNS = [
    ("tier_policies", "retrieval_price_gb", "NUMERIC(10,4) NOT NULL DEFAULT 0.2"),
    ("tier_policies", "request_price_1k", "NUMERIC(10,4) NOT NULL DEFAULT 6.0"),
    ("tier_policies", "restore_speed_gb_h", "NUMERIC(10,2) NOT NULL DEFAULT 500"),
    ("tier_policies", "restore_deadline_h", "NUMERIC(10,2) NOT NULL DEFAULT 8"),
    ("migration_plans", "retrieval_month", "NUMERIC(14,2) NOT NULL DEFAULT 0"),
    ("migration_plan_items", "target_tier", "VARCHAR(12) NOT NULL DEFAULT 'cold'"),
    ("migration_plan_items", "retrieval_month", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
    ("migration_plan_items", "restore_hours", "NUMERIC(10,2)"),
    ("migration_plan_items", "restore_ok", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("migration_plan_items", "restore_verdict", "VARCHAR(240) NOT NULL DEFAULT ''"),
    ("migration_plan_items", "stats_days", "INTEGER NOT NULL DEFAULT 0"),
    ("migration_plan_items", "stats_complete", "BOOLEAN NOT NULL DEFAULT TRUE"),
]


def upgrade() -> None:
    bind = op.get_bind()
    for table, column, ddl in COLUMNS:
        bind.exec_driver_sql(
            f'ALTER TABLE "{SCHEMA}"."{table}" ADD COLUMN IF NOT EXISTS "{column}" {ddl}'
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table, column, _ddl in reversed(COLUMNS):
        bind.exec_driver_sql(f'ALTER TABLE "{SCHEMA}"."{table}" DROP COLUMN IF EXISTS "{column}"')
