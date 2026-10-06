"""Trip-first realignment: trips and hangouts replace groups, series, travel, and visibility.

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
* ``plans.plans.kind`` becomes ``type`` (``trip`` | ``hangout``) plus an
  optional hangout ``activity`` (``kind = 'trip'`` maps to trips, every other
  kind to a hangout activity). Plans gain ``destinations`` (JSONB array of at
  most 10, trips only), ``pass_color`` (backfilled from the plan ID), and
  ``expected_size``.
* ``plans.plan_participants`` gains ``default_share`` (hundredths, default
  100), ``avatar_color`` (backfilled by join order), and ``capabilities``
  (``expenses.manage``, ``budgets.manage``). The participant write guard lets
  non-managers change only their own avatar colour; capabilities and default
  shares are a manager's call, and a self-inserted row carries none.
* ``iam.users`` gains ``default_currency``.

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
    SELECT count(*) FROM plans.plans WHERE type IS NULL OR pass_color IS NULL;  -- 0
    SELECT count(*) FROM plans.plan_participants WHERE avatar_color IS NULL;   -- 0

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


PASS_COLORS = "('indigo', 'plum', 'sea', 'forest', 'rust', 'slate', 'wine', 'moss')"
AVATAR_COLORS = "('blue', 'teal', 'purple', 'orange', 'rose', 'olive')"
ACTIVITIES = "('dinner', 'drinks', 'karaoke', 'coffee', 'movie', 'sport', 'birthday', 'other')"

ADD_SQL = f"""
-- Backfill updates fire the deferred owner checks; run them now so no trigger
-- events are pending when the following ALTER TABLE statements need the tables.
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE plans.plans
    ADD COLUMN type text,
    ADD COLUMN activity text CHECK (activity IN {ACTIVITIES}),
    ADD COLUMN destinations jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN pass_color text CHECK (pass_color IN {PASS_COLORS}),
    ADD COLUMN expected_size integer CHECK (expected_size BETWEEN 1 AND 50);
UPDATE plans.plans SET
    type = CASE WHEN kind = 'trip' THEN 'trip' ELSE 'hangout' END,
    activity = CASE kind
        WHEN 'trip' THEN NULL
        WHEN 'dinner' THEN 'dinner'
        WHEN 'coffee' THEN 'coffee'
        WHEN 'movie' THEN 'movie'
        WHEN 'sport' THEN 'sport'
        WHEN 'birthday' THEN 'birthday'
        ELSE 'other'
    END,
    pass_color = (ARRAY{PASS_COLORS.replace("(", "[").replace(")", "]")})[
        1 + mod(abs(hashtextextended(id::text, 0)), 8)::integer
    ];
ALTER TABLE plans.plans
    ALTER COLUMN type SET NOT NULL,
    ALTER COLUMN pass_color SET NOT NULL,
    ALTER COLUMN destinations DROP DEFAULT,
    ADD CONSTRAINT plans_type_check CHECK (type IN ('trip', 'hangout')),
    ADD CONSTRAINT plans_activity_for_hangouts CHECK (type = 'hangout' OR activity IS NULL),
    ADD CONSTRAINT plans_destinations_shape CHECK (
        jsonb_typeof(destinations) = 'array' AND jsonb_array_length(destinations) <= 10
    ),
    ADD CONSTRAINT plans_destinations_for_trips CHECK (
        type = 'trip' OR jsonb_array_length(destinations) = 0
    ),
    DROP COLUMN kind;

ALTER TABLE plans.plan_participants
    ADD COLUMN default_share integer NOT NULL DEFAULT 100
        CHECK (default_share BETWEEN 1 AND 10000),
    ADD COLUMN avatar_color text CHECK (avatar_color IN {AVATAR_COLORS}),
    ADD COLUMN capabilities text[] NOT NULL DEFAULT '{{}}'
        CHECK (capabilities <@ ARRAY['expenses.manage', 'budgets.manage']);
UPDATE plans.plan_participants AS p SET avatar_color = (ARRAY{AVATAR_COLORS.replace("(", "[").replace(")", "]")})[
    1 + mod(ordered.position - 1, 6)::integer
]
FROM (
    SELECT id, row_number() OVER (PARTITION BY plan_id ORDER BY created_at, id) AS position
    FROM plans.plan_participants
) AS ordered
WHERE ordered.id = p.id;
ALTER TABLE plans.plan_participants
    ALTER COLUMN avatar_color SET NOT NULL,
    ALTER COLUMN default_share DROP DEFAULT,
    ALTER COLUMN capabilities DROP DEFAULT;

ALTER TABLE iam.users ADD COLUMN default_currency char(3) CHECK (default_currency ~ '^[A-Z]{{3}}$');
"""

PARTICIPANT_GUARD_SQL = """
CREATE OR REPLACE FUNCTION plans.participant_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    actor_role text;
    invite_role text;
    claim_target uuid;
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.id <> OLD.id OR NEW.plan_id <> OLD.plan_id) THEN
        RAISE EXCEPTION 'participant scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    actor_role := plans.actor_plan_role(NEW.plan_id);
    IF TG_OP = 'UPDATE' AND (NEW.role = 'owner') <> (OLD.role = 'owner')
       AND actor_role IS DISTINCT FROM 'owner'
       AND NOT (NEW.role = 'owner' AND actor_role = 'admin'
                AND plans.owner_seat_vacant(NEW.plan_id)) THEN
        RAISE EXCEPTION 'only the owner can move ownership' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF actor_role IN ('owner', 'admin')
       OR (TG_OP = 'INSERT' AND plans.actor_is_unowned_plan_creator(NEW.plan_id)) THEN
        RETURN NEW;
    END IF;
    invite_role := plans.actor_held_invite_role(NEW.plan_id);
    IF TG_OP = 'INSERT' THEN
        IF NEW.user_id IS DISTINCT FROM actor
           OR NEW.role NOT IN ('member', 'guest', coalesce(invite_role, 'member'))
           OR NEW.access_state NOT IN ('active', 'pending_approval')
           OR NEW.capabilities <> '{}' OR NEW.default_share <> 100 THEN
            RAISE EXCEPTION 'participant insert not allowed'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    claim_target := plans.actor_held_claim_target(NEW.plan_id);
    IF OLD.user_id IS DISTINCT FROM actor
       AND NOT (OLD.id = claim_target AND OLD.identity_kind = 'placeholder') THEN
        RAISE EXCEPTION 'participant update not allowed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.capabilities IS DISTINCT FROM OLD.capabilities
       OR NEW.default_share IS DISTINCT FROM OLD.default_share THEN
        RAISE EXCEPTION 'participant settings are a manager''s call'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.role <> OLD.role
       AND NEW.role NOT IN ('member', 'guest', coalesce(invite_role, 'member')) THEN
        RAISE EXCEPTION 'participant role change not allowed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.access_state <> OLD.access_state AND NOT (
        NEW.access_state IN ('left', 'merged')
        OR (OLD.access_state = 'left'
            AND NEW.access_state IN ('active', 'pending_approval')
            AND (invite_role IS NOT NULL OR plans.actor_can_view_plan(NEW.plan_id)))
    ) THEN
        RAISE EXCEPTION 'participant access change not allowed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.user_id IS DISTINCT FROM OLD.user_id
       AND OLD.identity_kind NOT IN ('guest', 'placeholder') THEN
        RAISE EXCEPTION 'participant identity change not allowed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.execute(REWRITE_SQL)
    op.execute(SYNC_SQL)
    op.execute(DROP_SQL)
    op.execute(ADD_SQL)
    op.execute(PARTICIPANT_GUARD_SQL)


def downgrade() -> None:
    raise RuntimeError("The trip-first realignment is forward-only")
