"""Money alignment: expense time, the adjustment split, and readable revision history.

Revision ID: 000008_money_alignment
Revises: 000007_trip_first_realignment
Create Date: 2026-10-06

Forward action:

* ``finance.expense_revisions`` gains ``occurred_at`` and ``occurred_timezone``
  (both or neither; the API checks that ``occurred_on`` is the local date of
  ``occurred_at``), the split method ``adjustment``, and where each revision
  came from: ``source`` (``http`` or ``sync``), the device's
  ``client_created_at``, and the writing session's ``device_label``. Existing
  revisions read as ``http`` with no client time or device.

Lock/scan risk: ``ALTER TABLE`` takes ACCESS EXCLUSIVE on
``finance.expense_revisions`` briefly. New columns are nullable or carry a
constant default (catalog-only, no rewrite, no trigger runs); the default is
dropped right after. Replacing the split-method check scans the table once.

Validation:
    SELECT count(*) FROM finance.expense_revisions WHERE source IS NULL;          -- 0
    SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'expense_revisions_split_method_check';                     -- has 'adjustment'

Compatibility: additive for stored data. The API adds request and response
fields; old clients that send none of them keep working.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000008_money_alignment"
down_revision = "000007_trip_first_realignment"
branch_labels = None
depends_on = None

REVISIONS_SQL = """
ALTER TABLE finance.expense_revisions
    ADD COLUMN occurred_at timestamptz,
    ADD COLUMN occurred_timezone text CHECK (char_length(occurred_timezone) BETWEEN 1 AND 64),
    ADD COLUMN source text NOT NULL DEFAULT 'http' CHECK (source IN ('http', 'sync')),
    ADD COLUMN client_created_at timestamptz,
    ADD COLUMN device_label text CHECK (char_length(device_label) BETWEEN 1 AND 80),
    ADD CONSTRAINT expense_revisions_occurred_pair
        CHECK ((occurred_at IS NULL) = (occurred_timezone IS NULL)),
    DROP CONSTRAINT expense_revisions_split_method_check,
    ADD CONSTRAINT expense_revisions_split_method_check CHECK (
        split_method IN ('equal', 'exact', 'percentage', 'shares', 'itemized', 'adjustment')
    );
ALTER TABLE finance.expense_revisions ALTER COLUMN source DROP DEFAULT;
"""


def upgrade() -> None:
    op.execute(REVISIONS_SQL)


def downgrade() -> None:
    raise RuntimeError("Money alignment is forward-only")
