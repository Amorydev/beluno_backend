"""Activity feed and account deletion.

Revision ID: 000009_activity_and_deletion
Revises: 000008_money_alignment
Create Date: 2026-10-07

Forward action:

* New schema ``activity`` with append-only ``activity.events``: a readable record
  of what changed, in the plan scope (seen by the plan's active participants) or
  a user's own scope. Each event carries IDs and a small typed summary, never
  free text. Rows are written only through the SECURITY DEFINER gate
  ``activity.append_events``, which records the session's actor as the event's
  actor and requires that actor to be an active participant of the plan (someone
  who just left may record only their own leaving) or to own the user scope.
  ``activity.purge_events`` (worker) removes rows past the change-log retention.
* Account deletion: ``iam.users.status`` gains ``deleted`` and sessions gain the
  revoke reason ``account_deleted``. The SECURITY
  DEFINER gate ``people.forget_member`` removes the acting user from other
  people's crews (a crew left with nobody is tombstoned, so a deleted crew may
  hold no members) and returns the crews it changed so their owners get change
  rows. ``iam.forget_actor_credentials`` deletes the acting user's identities
  and pending email challenges (the API has no DELETE grant on them).
  ``iam.forget_merged_guests`` renames the guests merged into the acting account,
  and their merged participant rows, to "Former member".

Lock/scan risk: new objects, plus brief ACCESS EXCLUSIVE locks on ``iam.users``,
``iam.sessions``, and ``people.crews`` to replace one check constraint each
(one scan each).

Validation:
    SELECT relrowsecurity FROM pg_class WHERE oid = 'activity.events'::regclass;  -- t
    SELECT has_table_privilege('api_runtime', 'activity.events', 'INSERT');       -- f
    SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'users_status_check';                                         -- has 'deleted'

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000009_activity_and_deletion"
down_revision = "000008_money_alignment"
branch_labels = None
depends_on = None

ACTIVITY_SQL = """
CREATE SCHEMA activity;
REVOKE ALL ON SCHEMA activity FROM PUBLIC;
GRANT USAGE ON SCHEMA activity TO api_runtime, worker_runtime;

CREATE TABLE activity.events (
    id uuid PRIMARY KEY,
    scope_type text NOT NULL CHECK (scope_type IN ('plan', 'user')),
    scope_id uuid NOT NULL,
    plan_id uuid REFERENCES plans.plans (id),
    actor_user_id uuid REFERENCES iam.users (id),
    type text NOT NULL CHECK (type ~ '^[a-z_]+\\.[a-z_]+$'),
    entity_type text NOT NULL CHECK (entity_type ~ '^[a-z_]+$'),
    entity_id uuid NOT NULL,
    summary jsonb NOT NULL CHECK (jsonb_typeof(summary) = 'object'),
    occurred_at timestamptz NOT NULL,
    CHECK ((scope_type = 'plan') = (plan_id IS NOT NULL)),
    CHECK (scope_type <> 'plan' OR plan_id = scope_id)
);
CREATE INDEX events_scope_idx ON activity.events (scope_type, scope_id, id);
CREATE INDEX events_occurred_idx ON activity.events (occurred_at);

CREATE FUNCTION activity.reject_update() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'activity events are append-only' USING ERRCODE = 'insufficient_privilege';
END;
$$;
CREATE TRIGGER events_append_only BEFORE UPDATE ON activity.events
    FOR EACH ROW EXECUTE FUNCTION activity.reject_update();

ALTER TABLE activity.events ENABLE ROW LEVEL SECURITY;
CREATE POLICY events_select ON activity.events FOR SELECT TO api_runtime USING (
    (scope_type = 'plan' AND plans.actor_is_active_participant(scope_id))
    OR (scope_type = 'user' AND scope_id = iam.actor_id())
);
GRANT SELECT ON activity.events TO api_runtime;

-- The only write path. The session's actor is the event's actor. Plan events come
-- from the plan's active participants; someone who just left may record only that
-- they left (their own row, and nothing else in the summary), so people removed
-- or never approved cannot write into the feed. User events land only in the
-- actor's own scope.
CREATE FUNCTION activity.append_events(p_events jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    item jsonb;
    actor uuid := iam.actor_id();
BEGIN
    IF jsonb_typeof(p_events) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'events must be a JSON array' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(p_events) LOOP
        IF actor IS NULL
           OR NOT (
               (item->>'scope_type' = 'user' AND (item->>'scope_id')::uuid = actor)
               OR (item->>'scope_type' = 'plan'
                   AND plans.actor_is_active_participant((item->>'scope_id')::uuid))
               OR (item->>'scope_type' = 'plan'
                   AND item->>'type' = 'member.left'
                   AND item->>'entity_type' = 'plan_participant'
                   AND EXISTS (
                       SELECT 1 FROM plans.plan_participants AS p
                       WHERE p.id = (item->>'entity_id')::uuid
                         AND p.plan_id = (item->>'scope_id')::uuid
                         AND p.user_id = actor
                         AND p.access_state = 'left'
                         AND item->'summary' = jsonb_build_object('participant_id', p.id::text)
                   ))
           ) THEN
            RAISE EXCEPTION 'activity event outside the actor''s reach'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        INSERT INTO activity.events (
            id, scope_type, scope_id, plan_id, actor_user_id, type, entity_type, entity_id,
            summary, occurred_at
        ) VALUES (
            (item->>'id')::uuid,
            item->>'scope_type',
            (item->>'scope_id')::uuid,
            (item->>'plan_id')::uuid,
            actor,
            item->>'type',
            item->>'entity_type',
            (item->>'entity_id')::uuid,
            item->'summary',
            (item->>'occurred_at')::timestamptz
        );
    END LOOP;
END;
$$;

-- Retention: the worker removes events older than the change-log retention.
CREATE FUNCTION activity.purge_events(p_cutoff timestamptz, p_batch integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    removed integer;
BEGIN
    IF p_batch NOT BETWEEN 1 AND 100000 THEN
        RAISE EXCEPTION 'batch must be between 1 and 100000' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    DELETE FROM activity.events
    WHERE id IN (
        SELECT id FROM activity.events WHERE occurred_at < p_cutoff ORDER BY occurred_at
        LIMIT p_batch
    );
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;

REVOKE EXECUTE ON FUNCTION activity.reject_update(), activity.append_events(jsonb),
    activity.purge_events(timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION activity.append_events(jsonb) TO api_runtime;
GRANT EXECUTE ON FUNCTION activity.purge_events(timestamptz, integer) TO worker_runtime;
"""

DELETION_SQL = """
ALTER TABLE iam.users
    DROP CONSTRAINT users_status_check,
    ADD CONSTRAINT users_status_check CHECK (status IN ('active', 'disabled', 'deleted'));
ALTER TABLE iam.sessions
    DROP CONSTRAINT sessions_revoked_reason_check,
    ADD CONSTRAINT sessions_revoked_reason_check CHECK (
        revoked_reason IN (
            'logout', 'user_revoked', 'refresh_reuse', 'account_merged', 'account_deleted'
        )
    );

ALTER TABLE people.crews
    DROP CONSTRAINT crews_member_user_ids_check,
    ADD CONSTRAINT crews_member_user_ids_check CHECK (
        cardinality(member_user_ids) <= 50
        AND array_position(member_user_ids, NULL) IS NULL
        AND (cardinality(member_user_ids) >= 1 OR deleted_at IS NOT NULL)
    );

-- The acting user leaves every other person's crew (only their own ID is ever
-- removed). Returns each changed crew with its new version, and whether it was
-- tombstoned because nobody was left in it.
CREATE FUNCTION people.forget_member()
    RETURNS TABLE (crew_id uuid, owner_user_id uuid, version integer, removed boolean)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    actor uuid := iam.actor_id();
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'an actor is required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN QUERY
        UPDATE people.crews AS c
        SET member_user_ids = array_remove(c.member_user_ids, actor),
            deleted_at = CASE
                WHEN cardinality(array_remove(c.member_user_ids, actor)) = 0
                THEN transaction_timestamp() ELSE c.deleted_at END,
            version = c.version + 1,
            updated_at = transaction_timestamp()
        WHERE actor = ANY (c.member_user_ids)
          AND c.owner_user_id <> actor
          AND c.deleted_at IS NULL
        RETURNING c.id, c.owner_user_id, c.version, c.deleted_at IS NOT NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION people.forget_member() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION people.forget_member() TO api_runtime;

-- The acting user's sign-in identities and pending email challenges go; the API
-- holds no DELETE grant on these tables, and this gate touches only the actor's.
CREATE FUNCTION iam.forget_actor_credentials() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    actor_email text;
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'an actor is required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT email INTO actor_email FROM iam.users WHERE id = actor;
    DELETE FROM iam.user_identities WHERE user_id = actor;
    IF actor_email IS NOT NULL THEN
        DELETE FROM iam.email_challenges WHERE email = actor_email;
    END IF;
END;
$$;
REVOKE EXECUTE ON FUNCTION iam.forget_actor_credentials() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.forget_actor_credentials() TO api_runtime;

-- A guest merged into the acting account keeps its retired user row and its merged
-- participant rows under the guest's ID, and the write guards keep the API off
-- them. Both lose the guest-era name ("Former member", as the account does).
-- Returns the participant rows it changed with their new version, for change rows.
CREATE FUNCTION iam.forget_merged_guests()
    RETURNS TABLE (participant_id uuid, plan_id uuid, version integer)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    actor uuid := iam.actor_id();
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'an actor is required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE iam.users AS u
    SET display_name = 'Former member',
        status = 'deleted',
        version = u.version + 1,
        updated_at = transaction_timestamp()
    WHERE u.merged_into_user_id = actor;
    RETURN QUERY
        UPDATE plans.plan_participants AS p
        SET display_name = 'Former member',
            version = p.version + 1,
            updated_at = transaction_timestamp()
        WHERE p.user_id IN (SELECT u.id FROM iam.users AS u WHERE u.merged_into_user_id = actor)
        RETURNING p.id, p.plan_id, p.version;
END;
$$;
REVOKE EXECUTE ON FUNCTION iam.forget_merged_guests() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.forget_merged_guests() TO api_runtime;
"""


def upgrade() -> None:
    op.execute(ACTIVITY_SQL)
    op.execute(DELETION_SQL)


def downgrade() -> None:
    raise RuntimeError("The activity feed is forward-only")
