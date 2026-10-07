"""Support: problem reports with an optional diagnostic snapshot.

Revision ID: 000015_problem_reports
Revises: 000014_coordination
Create Date: 2026-10-07

Forward action:

* ``analytics_ops.problem_reports`` (in the schema created empty by the platform
  migration): who reported, a category (``balance_wrong``, ``sync_issue``,
  ``other``), an optional plan and linked record, the person's own words, and an
  optional diagnostic code (``BLN-XXXX-XXXX``) with its snapshot. The snapshot
  holds identifiers, states, versions, and sequence numbers only; never expense
  text, notes, or booking codes. Plan and record IDs carry no foreign key: a report
  outlives a purged plan.
* RLS: the API inserts reports in the actor's own name only and reads none; the
  worker role (operator script) reads them.
* ``finance.actor_ledger_problems(plan_id)``: for an active participant, how many
  disagreements ``finance.reconcile_plan`` finds (NULL for anyone else).
* ``analytics_ops.forget_problem_reports()``: account deletion removes the actor's
  reports and those of guests merged into the account.

Lock/scan risk: a new table whose foreign key takes a brief SHARE ROW EXCLUSIVE lock on
``iam.users``; two new functions. No existing row is read or rewritten.

Validation:
    SELECT relrowsecurity FROM pg_class
    WHERE oid = 'analytics_ops.problem_reports'::regclass;                     -- t
    SELECT has_table_privilege('api_runtime', 'analytics_ops.problem_reports',
                               'SELECT');                                        -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000015_problem_reports"
down_revision = "000014_coordination"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE analytics_ops.problem_reports (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES iam.users (id),
    category text NOT NULL CHECK (category IN ('balance_wrong', 'sync_issue', 'other')),
    plan_id uuid,
    entity_type text CHECK (
        entity_type IN ('expense', 'settlement', 'budget', 'place', 'itinerary_item',
                        'poll', 'booking', 'task', 'packing_item')
    ),
    entity_id uuid,
    message text NOT NULL CHECK (char_length(btrim(message)) BETWEEN 1 AND 1000),
    diagnostic_code text UNIQUE
        CHECK (diagnostic_code ~ '^BLN-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$'),
    diagnostics jsonb,
    created_at timestamptz NOT NULL,
    CHECK ((entity_type IS NULL) = (entity_id IS NULL)),
    CHECK (entity_id IS NULL OR plan_id IS NOT NULL),
    CHECK ((diagnostic_code IS NULL) = (diagnostics IS NULL))
);
CREATE INDEX problem_reports_created_idx ON analytics_ops.problem_reports (created_at DESC);
CREATE INDEX problem_reports_user_idx ON analytics_ops.problem_reports (user_id);

ALTER TABLE analytics_ops.problem_reports ENABLE ROW LEVEL SECURITY;
CREATE POLICY problem_reports_insert ON analytics_ops.problem_reports
    FOR INSERT TO api_runtime WITH CHECK (user_id = iam.actor_id());
CREATE POLICY problem_reports_operator_select ON analytics_ops.problem_reports
    FOR SELECT TO worker_runtime USING (true);
GRANT INSERT ON analytics_ops.problem_reports TO api_runtime;
GRANT SELECT ON analytics_ops.problem_reports TO worker_runtime;
"""

FUNCTIONS_SQL = """
-- How many disagreements reconciling the plan's ledger finds, for an active
-- participant only (NULL for anyone else); the rows themselves stay hidden.
CREATE FUNCTION finance.actor_ledger_problems(p_plan_id uuid) RETURNS integer
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT plans.actor_is_active_participant(p_plan_id) THEN
        RETURN NULL;
    END IF;
    RETURN (SELECT count(*) FROM finance.reconcile_plan(p_plan_id));
END;
$$;
REVOKE EXECUTE ON FUNCTION finance.actor_ledger_problems(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION finance.actor_ledger_problems(uuid) TO api_runtime;

-- Account deletion removes the person's reports (their own words), including those
-- of guests merged into the account.
CREATE FUNCTION analytics_ops.forget_problem_reports() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    removed integer;
BEGIN
    -- Also what guests merged into this account sent before they signed in.
    DELETE FROM analytics_ops.problem_reports
    WHERE user_id = iam.actor_id()
       OR user_id IN (SELECT id FROM iam.users WHERE merged_into_user_id = iam.actor_id());
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;
REVOKE EXECUTE ON FUNCTION analytics_ops.forget_problem_reports() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics_ops.forget_problem_reports() TO api_runtime;
"""


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(FUNCTIONS_SQL)


def downgrade() -> None:
    raise RuntimeError("Problem reports are forward-only")
