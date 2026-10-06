"""Trip-first realignment: remove groups, plan series, travel details, and plan visibility.

Revision ID: 000007_trip_first_realignment
Revises: 000006_merged_guest_authorship
Create Date: 2026-10-06

Forward action (ADR 0008): the app is trip-first (trips, hangouts, crews), so
the durable group container, recurring plan series, the travel extension, and
group-visible plans go away.

* Functions and policies that referenced them are rewritten without those
  branches: ``iam.actor_shares_context`` (shared plans only),
  ``plans.actor_can_view_plan`` (active participants only),
  ``plans.plan_write_guard``, ``sync_audit.actor_can_view_scope``, and the
  ``plans_insert`` policy.
* Group-scope change rows and heads are deleted; ``scope_type`` allows
  ``user`` and ``plan`` only. ``audit_events.group_id`` is dropped.
* ``plans.plans`` loses ``group_id``, ``series_id``, ``occurrence_key``,
  ``is_series_exception``, and ``visibility`` (their checks and indexes go
  with them).
* Tables ``plans.travel_segments``, ``plans.travel_plan_details``,
  ``plans.plan_series``, ``groups.group_invites``,
  ``groups.group_memberships``, and ``groups.groups`` are dropped, then every
  ``groups`` function and the empty ``groups`` schema. Drops are explicit; no
  ``CASCADE``.
* Queued ``plans.extend_series_horizons`` jobs are removed (the task no longer
  exists).

Dependents were listed before writing this migration with:
    SELECT n.nspname || '.' || p.proname FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.prosrc ~ 'groups\\.|plan_series|series_id|travel_|visibility|group_id|''group''';
    SELECT schemaname, tablename, policyname FROM pg_policies
    WHERE coalesce(qual, '') || coalesce(with_check, '') ~ 'groups\\.|group_id|visibility';

Lock/scan risk: ACCESS EXCLUSIVE locks on the dropped tables and on
``plans.plans``, ``sync_audit.change_log``, ``sync_audit.scope_heads``, and
``sync_audit.audit_events`` while columns and checks change. Tables are small
before launch; run in a maintenance window otherwise.

Validation:
    SELECT to_regnamespace('groups');                                          -- NULL
    SELECT to_regclass('plans.plan_series'), to_regclass('plans.travel_segments');
                                                                                -- NULL, NULL
    SELECT count(*) FROM information_schema.columns WHERE table_schema = 'plans'
      AND table_name = 'plans'
      AND column_name IN ('group_id', 'series_id', 'visibility');              -- 0
    SELECT count(*) FROM sync_audit.change_log WHERE scope_type = 'group';     -- 0

Compatibility: breaking by design and accepted before launch (the client has
not integrated groups, series, travel, or visibility). The API and worker must
be deployed with this revision.

Rollback: forward-only. Restore the pre-migration backup if it fails in a
shared environment; never re-create groups by reversing it.
"""

from alembic import op

revision = "000007_trip_first_realignment"
down_revision = "000006_merged_guest_authorship"
branch_labels = None
depends_on = None

REWRITE_SQL = """
CREATE OR REPLACE FUNCTION iam.actor_shares_context(p_user_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
        FROM plans.plan_participants mine
        JOIN plans.plan_participants theirs ON theirs.plan_id = mine.plan_id
        WHERE mine.user_id = iam.actor_id() AND mine.access_state = 'active'
          AND theirs.user_id = p_user_id
    )
$$;

CREATE OR REPLACE FUNCTION plans.actor_can_view_plan(p_plan_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT plans.actor_is_active_participant(p_plan_id)
$$;

CREATE OR REPLACE FUNCTION plans.plan_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.created_by_user_id <> OLD.created_by_user_id THEN
        RAISE EXCEPTION 'plan scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF plans.actor_plan_role(NEW.id) IS DISTINCT FROM 'owner'
       AND plans.actor_plan_role(NEW.id) IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'plan update not allowed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sync_audit.actor_can_view_scope(p_scope_type text, p_scope_id uuid)
    RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT CASE p_scope_type
        WHEN 'user' THEN p_scope_id = iam.actor_id()
        WHEN 'plan' THEN plans.actor_can_view_plan(p_scope_id)
        ELSE false
    END
$$;

DROP POLICY plans_insert ON plans.plans;
CREATE POLICY plans_insert ON plans.plans FOR INSERT TO api_runtime
    WITH CHECK (created_by_user_id = iam.actor_id());
"""

SYNC_SQL = """
DELETE FROM sync_audit.change_log WHERE scope_type = 'group';
DELETE FROM sync_audit.scope_heads WHERE scope_type = 'group';
ALTER TABLE sync_audit.change_log DROP CONSTRAINT change_log_scope_type_check;
ALTER TABLE sync_audit.change_log
    ADD CONSTRAINT change_log_scope_type_check CHECK (scope_type IN ('user', 'plan'));
ALTER TABLE sync_audit.scope_heads DROP CONSTRAINT scope_heads_scope_type_check;
ALTER TABLE sync_audit.scope_heads
    ADD CONSTRAINT scope_heads_scope_type_check CHECK (scope_type IN ('user', 'plan'));
ALTER TABLE sync_audit.audit_events DROP COLUMN group_id;
"""

DROP_SQL = """
DROP TABLE plans.travel_segments;
DROP TABLE plans.travel_plan_details;

ALTER TABLE plans.plans
    DROP COLUMN series_id,
    DROP COLUMN occurrence_key,
    DROP COLUMN is_series_exception,
    DROP COLUMN group_id,
    DROP COLUMN visibility;

DROP TABLE plans.plan_series;
DROP FUNCTION plans.series_write_guard();

DROP TABLE groups.group_invites;
DROP TABLE groups.group_memberships;
DROP TABLE groups.groups;
DROP FUNCTION groups.actor_can_view_group(uuid);
DROP FUNCTION groups.actor_group_role(uuid);
DROP FUNCTION groups.actor_held_invite_role(uuid);
DROP FUNCTION groups.actor_holds_invite(uuid);
DROP FUNCTION groups.actor_is_active_member(uuid);
DROP FUNCTION groups.actor_is_unowned_group_creator(uuid);
DROP FUNCTION groups.enforce_single_owner();
DROP FUNCTION groups.group_invite_write_guard();
DROP FUNCTION groups.group_write_guard();
DROP FUNCTION groups.membership_write_guard();
DROP FUNCTION groups.owner_seat_vacant(uuid);
DROP SCHEMA groups;

DO $$
BEGIN
    IF to_regclass('jobs.procrastinate_jobs') IS NOT NULL THEN
        DELETE FROM jobs.procrastinate_jobs
        WHERE task_name = 'plans.extend_series_horizons' AND status = 'todo';
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(REWRITE_SQL)
    op.execute(SYNC_SQL)
    op.execute(DROP_SQL)


def downgrade() -> None:
    raise RuntimeError("The trip-first realignment is forward-only")
