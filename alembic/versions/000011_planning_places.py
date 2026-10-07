"""Trip planning: places and the itinerary.

Revision ID: 000011_planning_places
Revises: 000010_retention_purges
Create Date: 2026-10-07

Forward action:

* In the ``schedule_places`` schema (created empty by the platform migration): ``places`` (saved places with an optional Maps link,
  parsed offline for coordinates), ``place_reactions`` ("want to go", one row per
  participant), ``itinerary_items`` (a day or anytime, an optional local start
  time with its zone, ordered by a fractional key within the day), and
  ``item_attendance`` (going / not going, one row per participant).
* RLS: the plan's active participants read, insert, and update; nobody deletes
  (places and items are tombstoned, reactions and attendance are updated). Write
  guards keep identity columns fixed, require ``version + 1`` on places and
  items, refuse changes to tombstones, and let a participant write only their
  own reaction and attendance rows.
* ``plans.purge_deleted_plan`` also removes the plan's planning rows.
* ``finance.cost_commitments.converted_from_state`` may be ``cancelled``: a cost
  an expense paid remembers that its source (an item, later a booking) was
  withdrawn, so voiding that expense cancels the cost instead of reviving it.

Lock/scan risk: new tables; creating their foreign keys takes brief SHARE ROW EXCLUSIVE
locks on ``plans.plans``, ``plans.plan_participants``, and ``iam.users``; the purge
function is replaced (no table lock); one check constraint on
``finance.cost_commitments`` is replaced (brief ACCESS EXCLUSIVE lock, one scan).

Validation:
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'schedule_places' AND c.relkind = 'r' AND c.relrowsecurity;      -- 4
    SELECT has_table_privilege('api_runtime', 'schedule_places.places', 'DELETE');     -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000011_planning_places"
down_revision = "000010_retention_purges"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE schedule_places.places (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    maps_url text CHECK (char_length(maps_url) BETWEEN 1 AND 2000),
    provider text CHECK (provider IN ('google', 'apple', 'osm')),
    latitude numeric(9, 6) CHECK (latitude BETWEEN -90 AND 90),
    longitude numeric(9, 6) CHECK (longitude BETWEEN -180 AND 180),
    resolution_state text NOT NULL CHECK (resolution_state IN ('manual', 'parsed', 'pending')),
    category text NOT NULL CHECK (
        category IN ('food', 'sight', 'stay', 'activity', 'shopping', 'nightlife', 'other')
    ),
    note text CHECK (char_length(note) <= 1000),
    status text NOT NULL CHECK (status IN ('shortlist', 'in_plan', 'poll_winner')),
    saved_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    CHECK ((latitude IS NULL) = (longitude IS NULL)),
    CHECK ((resolution_state = 'parsed') = (latitude IS NOT NULL)),
    CHECK ((resolution_state = 'manual') = (maps_url IS NULL))
);
CREATE INDEX places_plan_idx ON schedule_places.places (plan_id, id);

CREATE TABLE schedule_places.place_reactions (
    place_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    participant_id uuid NOT NULL,
    wants boolean NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (place_id, participant_id),
    FOREIGN KEY (plan_id, place_id) REFERENCES schedule_places.places (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);
CREATE INDEX place_reactions_plan_idx ON schedule_places.place_reactions (plan_id, participant_id);

CREATE TABLE schedule_places.itinerary_items (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    day date,
    start_time time,
    timezone text CHECK (char_length(timezone) BETWEEN 1 AND 64),
    duration_minutes integer CHECK (duration_minutes BETWEEN 1 AND 10080),
    title text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
    note text CHECK (char_length(note) <= 2000),
    place_id uuid,
    lead_participant_id uuid,
    status text NOT NULL CHECK (status IN ('planned', 'done', 'cancelled')),
    -- Fractional keys compare byte by byte, whatever the database's default collation.
    order_key text COLLATE "C" NOT NULL CHECK (char_length(order_key) BETWEEN 1 AND 128),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, place_id) REFERENCES schedule_places.places (plan_id, id),
    FOREIGN KEY (plan_id, lead_participant_id) REFERENCES plans.plan_participants (plan_id, id),
    CHECK ((start_time IS NULL) = (timezone IS NULL)),
    CHECK (start_time IS NULL OR day IS NOT NULL)
);
CREATE INDEX itinerary_items_day_idx ON schedule_places.itinerary_items (plan_id, day, order_key, id);
CREATE INDEX itinerary_items_place_idx ON schedule_places.itinerary_items (plan_id, place_id)
    WHERE place_id IS NOT NULL;

CREATE TABLE schedule_places.item_attendance (
    item_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    participant_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('going', 'not_going')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (item_id, participant_id),
    FOREIGN KEY (plan_id, item_id) REFERENCES schedule_places.itinerary_items (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);
CREATE INDEX item_attendance_plan_idx ON schedule_places.item_attendance (plan_id, participant_id);
"""

GUARDS_SQL = """
-- Places and items: identity fixed, one version step per change, tombstones final.
CREATE FUNCTION schedule_places.guard_root() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.plan_id <> OLD.plan_id OR NEW.created_at <> OLD.created_at
       OR (to_jsonb(NEW) ->> 'saved_by_user_id')
          IS DISTINCT FROM (to_jsonb(OLD) ->> 'saved_by_user_id')
       OR (to_jsonb(NEW) ->> 'created_by_user_id')
          IS DISTINCT FROM (to_jsonb(OLD) ->> 'created_by_user_id')
       OR OLD.deleted_at IS NOT NULL
       OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'schedule_places.% does not allow this change', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

-- Reactions and attendance: each participant answers only for themselves.
CREATE FUNCTION schedule_places.guard_answer() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.plan_id <> OLD.plan_id OR NEW.participant_id <> OLD.participant_id
        OR NEW.created_at <> OLD.created_at
        OR (to_jsonb(NEW) ->> 'place_id') IS DISTINCT FROM (to_jsonb(OLD) ->> 'place_id')
        OR (to_jsonb(NEW) ->> 'item_id') IS DISTINCT FROM (to_jsonb(OLD) ->> 'item_id')
    ) THEN
        RAISE EXCEPTION 'schedule_places.% does not allow this change', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = NEW.plan_id AND id = NEW.participant_id AND user_id = iam.actor_id()
    ) THEN
        RAISE EXCEPTION 'participants answer only for themselves'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER places_guard BEFORE UPDATE ON schedule_places.places
    FOR EACH ROW EXECUTE FUNCTION schedule_places.guard_root();
CREATE TRIGGER itinerary_items_guard BEFORE UPDATE ON schedule_places.itinerary_items
    FOR EACH ROW EXECUTE FUNCTION schedule_places.guard_root();
CREATE TRIGGER place_reactions_guard BEFORE INSERT OR UPDATE ON schedule_places.place_reactions
    FOR EACH ROW EXECUTE FUNCTION schedule_places.guard_answer();
CREATE TRIGGER item_attendance_guard BEFORE INSERT OR UPDATE ON schedule_places.item_attendance
    FOR EACH ROW EXECUTE FUNCTION schedule_places.guard_answer();
REVOKE EXECUTE ON FUNCTION schedule_places.guard_root(), schedule_places.guard_answer() FROM PUBLIC;
"""

PLANNING_TABLES = ("places", "place_reactions", "itinerary_items", "item_attendance")

RLS_SQL = "\n".join(
    f"ALTER TABLE schedule_places.{table} ENABLE ROW LEVEL SECURITY;\n"
    f"CREATE POLICY {table}_select ON schedule_places.{table} FOR SELECT TO api_runtime\n"
    f"    USING (plans.actor_is_active_participant(plan_id));\n"
    f"CREATE POLICY {table}_insert ON schedule_places.{table} FOR INSERT TO api_runtime\n"
    f"    WITH CHECK (plans.actor_is_active_participant(plan_id));\n"
    f"CREATE POLICY {table}_update ON schedule_places.{table} FOR UPDATE TO api_runtime\n"
    f"    USING (plans.actor_is_active_participant(plan_id))\n"
    f"    WITH CHECK (plans.actor_is_active_participant(plan_id));\n"
    f"GRANT SELECT, INSERT, UPDATE ON schedule_places.{table} TO api_runtime;"
    for table in PLANNING_TABLES
)

PURGE_SQL = """
CREATE OR REPLACE FUNCTION plans.purge_deleted_plan(p_cutoff timestamptz) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    target uuid;
    now_at timestamptz := transaction_timestamp();
    crew_changes jsonb;
BEGIN
    IF p_cutoff IS NULL OR p_cutoff > now_at - interval '7 days' THEN
        RAISE EXCEPTION 'plans are purged no sooner than seven days after deletion is scheduled'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- A restore holds the plan row; skip it rather than wait, the next run decides.
    SELECT id INTO target
    FROM plans.plans
    WHERE deletion_scheduled_at IS NOT NULL AND deletion_scheduled_at <= p_cutoff
    ORDER BY deletion_scheduled_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED;
    IF target IS NULL THEN
        RETURN NULL;
    END IF;

    -- Devices of everyone who had the plan learn it is gone.
    PERFORM sync_audit.append_changes(coalesce((
        SELECT jsonb_agg(jsonb_build_object(
                   'changed_at', now_at,
                   'scope_type', 'user',
                   'scope_id', p.user_id,
                   'entity_type', 'plan_access',
                   'entity_id', target,
                   'entity_version', p.version + 1,
                   'operation', 'delete'
               ) ORDER BY p.user_id)
        FROM plans.plan_participants AS p
        WHERE p.plan_id = target AND p.user_id IS NOT NULL AND p.access_state <> 'merged'
    ), '[]'::jsonb));

    PERFORM set_config('beluno.plan_purge', 'on', true);
    -- Children before parents. Revisions go before expenses and commitments: the
    -- expense's current-revision key is deferred, which breaks that cycle.
    DELETE FROM finance.account_balances WHERE plan_id = target;
    DELETE FROM finance.ledger_postings WHERE plan_id = target;
    DELETE FROM finance.ledger_confirmations WHERE plan_id = target;
    DELETE FROM finance.ledger_transactions WHERE plan_id = target;
    DELETE FROM finance.consolidation_lines WHERE plan_id = target;
    DELETE FROM finance.consolidation_rates WHERE plan_id = target;
    DELETE FROM finance.consolidations WHERE plan_id = target;
    DELETE FROM finance.base_currency_changes WHERE plan_id = target;
    DELETE FROM finance.refund_shares WHERE plan_id = target;
    DELETE FROM finance.expense_refunds WHERE plan_id = target;
    DELETE FROM finance.expense_payers WHERE plan_id = target;
    DELETE FROM finance.expense_splits WHERE plan_id = target;
    DELETE FROM finance.expense_revisions WHERE plan_id = target;
    DELETE FROM finance.cost_commitments WHERE plan_id = target;
    DELETE FROM finance.expenses WHERE plan_id = target;
    DELETE FROM finance.fund_counts WHERE plan_id = target;
    DELETE FROM finance.fund_movements WHERE plan_id = target;
    DELETE FROM finance.fund_settings WHERE plan_id = target;
    DELETE FROM finance.budgets WHERE plan_id = target;
    DELETE FROM finance.settlements WHERE plan_id = target;
    DELETE FROM finance.fx_snapshots WHERE plan_id = target;
    DELETE FROM finance.ledger_accounts WHERE plan_id = target;
    DELETE FROM finance.plan_ledger_heads WHERE plan_id = target;
    PERFORM set_config('beluno.plan_purge', '', true);

    DELETE FROM activity.events WHERE plan_id = target;
    DELETE FROM schedule_places.item_attendance WHERE plan_id = target;
    DELETE FROM schedule_places.itinerary_items WHERE plan_id = target;
    DELETE FROM schedule_places.place_reactions WHERE plan_id = target;
    DELETE FROM schedule_places.places WHERE plan_id = target;
    -- Participants and invites point at each other (who joined through which link).
    UPDATE plans.plan_participants SET joined_via_invite_id = NULL
    WHERE plan_id = target AND joined_via_invite_id IS NOT NULL;
    DELETE FROM plans.plan_invites WHERE plan_id = target;
    DELETE FROM plans.plan_participants WHERE plan_id = target;
    DELETE FROM sync_audit.change_log WHERE scope_type = 'plan' AND scope_id = target;
    DELETE FROM sync_audit.scope_heads WHERE scope_type = 'plan' AND scope_id = target;
    -- Copies keep their content; only the (unexposed) link to their source goes.
    UPDATE plans.plans SET duplicated_from_plan_id = NULL WHERE duplicated_from_plan_id = target;
    -- Crews started from the plan forget it, and their owners' devices hear of it.
    WITH changed AS (
        UPDATE people.crews
        SET source_plan_id = NULL, version = version + 1, updated_at = now_at
        WHERE source_plan_id = target
        RETURNING id, owner_user_id, version, deleted_at
    )
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'changed_at', now_at,
               'scope_type', 'user',
               'scope_id', owner_user_id,
               'entity_type', 'crew',
               'entity_id', id,
               'entity_version', version,
               'operation', CASE WHEN deleted_at IS NULL THEN 'upsert' ELSE 'delete' END
           ) ORDER BY id), '[]'::jsonb)
    INTO crew_changes
    FROM changed;
    PERFORM sync_audit.append_changes(crew_changes);
    DELETE FROM plans.plans WHERE id = target;
    INSERT INTO sync_audit.audit_events (
        id, occurred_at, action, entity_type, entity_id, plan_id, metadata
    ) VALUES (
        gen_random_uuid(), now_at, 'plan.purged', 'plan', target, target, '{}'::jsonb
    );
    RETURN target;
END;
$$;
"""


FINANCE_SQL = """
ALTER TABLE finance.cost_commitments
    DROP CONSTRAINT cost_commitments_converted_from_state_check,
    ADD CONSTRAINT cost_commitments_converted_from_state_check
        CHECK (converted_from_state IN ('estimated', 'committed', 'cancelled'));
"""


def upgrade() -> None:
    op.execute(FINANCE_SQL)
    op.execute(TABLES_SQL)
    op.execute(GUARDS_SQL)
    op.execute(RLS_SQL)
    op.execute(PURGE_SQL)


def downgrade() -> None:
    raise RuntimeError("Places and the itinerary are forward-only")
