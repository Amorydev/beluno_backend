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
  actor and requires that actor to have a participant row in the plan (people
  who just left included) or to own the user scope. ``activity.purge_events`` (worker) removes rows past the
  change-log retention.

Lock/scan risk: new objects only.

Validation:
    SELECT relrowsecurity FROM pg_class WHERE oid = 'activity.events'::regclass;  -- t
    SELECT has_table_privilege('api_runtime', 'activity.events', 'INSERT');       -- f

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

-- The only write path. The session's actor is the event's actor; events land in a
-- plan they have a row in (someone who just left still records leaving) or in
-- their own scope.
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
               (item->>'scope_type' = 'plan'
                AND plans.actor_has_participant_row((item->>'scope_id')::uuid))
               OR (item->>'scope_type' = 'user' AND (item->>'scope_id')::uuid = actor)
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


def upgrade() -> None:
    op.execute(ACTIVITY_SQL)


def downgrade() -> None:
    raise RuntimeError("The activity feed is forward-only")
