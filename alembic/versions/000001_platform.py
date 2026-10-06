"""Create Beluno PostgreSQL schemas.

Revision ID: 000001_platform
Revises:
Create Date: 2026-10-06

This is an expand-only foundation migration. Runtime database roles are
provisioned by deployment infrastructure because managed providers differ in
whether application migrations may create principals.
"""

from alembic import op

revision = "000001_platform"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = (
    "iam",
    "groups",
    "plans",
    "sync_audit",
    "finance",
    "decisions",
    "schedule_places",
    "coordination",
    "bookings",
    "media_memories",
    "engagement",
    "search_export",
    "billing",
    "analytics_ops",
    "jobs",
)


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


def downgrade() -> None:
    raise RuntimeError("Platform schemas are intentionally forward-only")
