"""Trip planning: bookings, with their secrets encrypted at rest.

Revision ID: 000013_bookings
Revises: 000012_decisions_polls
Create Date: 2026-10-07

Forward action:

* In the ``bookings`` schema (created empty by the platform migration):
  ``bookings`` (kind, title, provider, local start and end with their zones, an
  optional place, traveler participant IDs, status ``planned``/``confirmed``/
  ``cancelled``, payment note, free-cancellation deadline) and ``booking_secrets``
  (AES-GCM ciphertexts of the confirmation code and private notes, with the key
  they were sealed with; the API holds the keys, the database never sees plaintext).
* RLS: the plan's active participants read and write bookings; nobody deletes
  (tombstones). Booking secrets are read and written only by whoever may reveal
  them: the booking's travelers, its creator (or a guest merged into them), and
  the plan's owner/admins (``bookings.actor_may_reveal``; a traveler merged into
  another participant counts as that participant). Guards: only the creator or an
  organiser changes a booking (``bookings.actor_manages_booking``), identities stay
  fixed, every change is ``version + 1``, and every traveler is a participant of
  the plan.
* ``schedule_places.itinerary_items`` gains ``booking_id`` (a booking of the same
  plan).
* ``plans.purge_deleted_plan`` also removes the plan's bookings (after its items,
  before its places).

Lock/scan risk: new tables; one nullable column and a foreign key on
``schedule_places.itinerary_items`` (brief ACCESS EXCLUSIVE lock, no rewrite; the
key validates against an empty or small table); the purge function is replaced.

Validation:
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'bookings' AND c.relkind = 'r' AND c.relrowsecurity;      -- 2
    SELECT has_table_privilege('api_runtime', 'bookings.booking_secrets', 'DELETE'); -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000013_bookings"
down_revision = "000012_decisions_polls"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE bookings.bookings (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    kind text NOT NULL CHECK (
        kind IN ('flight', 'lodging', 'transport', 'activity', 'restaurant', 'insurance', 'other')
    ),
    title text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
    provider text CHECK (char_length(provider) BETWEEN 1 AND 120),
    start_date date,
    start_time time,
    start_timezone text CHECK (char_length(start_timezone) BETWEEN 1 AND 64),
    end_date date,
    end_time time,
    end_timezone text CHECK (char_length(end_timezone) BETWEEN 1 AND 64),
    place_id uuid,
    traveler_ids uuid[] NOT NULL CHECK (
        cardinality(traveler_ids) <= 50 AND array_position(traveler_ids, NULL) IS NULL
    ),
    status text NOT NULL CHECK (status IN ('planned', 'confirmed', 'cancelled')),
    payment_note text CHECK (
        payment_note IN ('prepaid', 'pay_at_property', 'each_paid_own', 'personal')
    ),
    free_cancellation_until timestamptz,
    -- Whether each secret is set, readable by everyone who sees the booking.
    has_confirmation_code boolean NOT NULL,
    has_private_notes boolean NOT NULL,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, place_id) REFERENCES schedule_places.places (plan_id, id),
    CHECK ((start_time IS NULL) = (start_timezone IS NULL)),
    CHECK (start_time IS NULL OR start_date IS NOT NULL),
    CHECK ((end_time IS NULL) = (end_timezone IS NULL)),
    CHECK (end_time IS NULL OR end_date IS NOT NULL),
    CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
);
CREATE INDEX bookings_plan_idx ON bookings.bookings (plan_id, start_date, id);

CREATE TABLE bookings.booking_secrets (
    booking_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL,
    key_id text NOT NULL CHECK (char_length(key_id) BETWEEN 1 AND 64),
    confirmation_code bytea CHECK (octet_length(confirmation_code) BETWEEN 29 AND 1024),
    private_notes bytea CHECK (octet_length(private_notes) BETWEEN 29 AND 8192),
    updated_at timestamptz NOT NULL,
    FOREIGN KEY (plan_id, booking_id) REFERENCES bookings.bookings (plan_id, id)
);
CREATE INDEX booking_secrets_plan_idx ON bookings.booking_secrets (plan_id);

ALTER TABLE schedule_places.itinerary_items
    ADD COLUMN booking_id uuid,
    ADD FOREIGN KEY (plan_id, booking_id) REFERENCES bookings.bookings (plan_id, id);
CREATE INDEX itinerary_items_booking_idx ON schedule_places.itinerary_items (plan_id, booking_id)
    WHERE booking_id IS NOT NULL;
"""

GUARDS_SQL = """
-- Who may see a booking's secrets: its travelers, its creator (or a guest account
-- merged into the actor), and the plan's owner/admins, while active in the plan.
CREATE FUNCTION bookings.actor_may_reveal(p_booking_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
        FROM bookings.bookings AS b
        JOIN plans.plan_participants AS p
          ON p.plan_id = b.plan_id AND p.user_id = iam.actor_id() AND p.access_state = 'active'
        WHERE b.id = p_booking_id AND b.deleted_at IS NULL
          AND (p.role IN ('owner', 'admin') OR p.id = ANY (b.traveler_ids)
               -- a traveler whose row was later merged into the actor's
               OR EXISTS (SELECT 1 FROM plans.plan_participants AS m
                          WHERE m.plan_id = b.plan_id AND m.merged_into_participant_id = p.id
                            AND m.id = ANY (b.traveler_ids))
               OR b.created_by_user_id = iam.actor_id()
               OR b.created_by_user_id IN (SELECT id FROM iam.users
                                           WHERE merged_into_user_id = iam.actor_id()))
    )
$$;
REVOKE EXECUTE ON FUNCTION bookings.actor_may_reveal(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bookings.actor_may_reveal(uuid) TO api_runtime;

-- Who may change a booking: its creator (or a guest merged into the actor) and the
-- plan's active owner/admins.
CREATE FUNCTION bookings.actor_manages_booking(p_plan_id uuid, p_creator uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND user_id = iam.actor_id() AND access_state = 'active'
          AND (role IN ('owner', 'admin') OR p_creator = iam.actor_id()
               OR p_creator IN (SELECT id FROM iam.users
                                WHERE merged_into_user_id = iam.actor_id()))
    )
$$;
REVOKE EXECUTE ON FUNCTION bookings.actor_manages_booking(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bookings.actor_manages_booking(uuid, uuid) TO api_runtime;

CREATE FUNCTION bookings.guard_booking() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND (NEW.version <> 1 OR NEW.deleted_at IS NOT NULL
                             OR NEW.created_by_user_id IS DISTINCT FROM iam.actor_id()) THEN
        RAISE EXCEPTION 'a booking is added in its creator''s name'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        (NEW.id, NEW.plan_id, NEW.created_by_user_id, NEW.created_at)
            IS DISTINCT FROM (OLD.id, OLD.plan_id, OLD.created_by_user_id, OLD.created_at)
        OR OLD.deleted_at IS NOT NULL OR NEW.version <> OLD.version + 1
        OR NOT bookings.actor_manages_booking(OLD.plan_id, OLD.created_by_user_id)
    ) THEN
        RAISE EXCEPTION 'bookings.bookings does not allow this change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(NEW.traveler_ids) AS t(id)
        WHERE NOT EXISTS (
            SELECT 1 FROM plans.plan_participants WHERE plan_id = NEW.plan_id AND id = t.id
        )
    ) THEN
        RAISE EXCEPTION 'travelers are participants of the plan'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER bookings_guard BEFORE INSERT OR UPDATE ON bookings.bookings
    FOR EACH ROW EXECUTE FUNCTION bookings.guard_booking();
REVOKE EXECUTE ON FUNCTION bookings.guard_booking() FROM PUBLIC;
"""

RLS_SQL = """
ALTER TABLE bookings.bookings ENABLE ROW LEVEL SECURITY;
CREATE POLICY bookings_select ON bookings.bookings FOR SELECT TO api_runtime
    USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY bookings_insert ON bookings.bookings FOR INSERT TO api_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY bookings_update ON bookings.bookings FOR UPDATE TO api_runtime
    USING (plans.actor_is_active_participant(plan_id))
    WITH CHECK (plans.actor_is_active_participant(plan_id));
GRANT SELECT, INSERT, UPDATE ON bookings.bookings TO api_runtime;

ALTER TABLE bookings.booking_secrets ENABLE ROW LEVEL SECURITY;
CREATE POLICY booking_secrets_select ON bookings.booking_secrets FOR SELECT TO api_runtime
    USING (bookings.actor_may_reveal(booking_id));
CREATE POLICY booking_secrets_insert ON bookings.booking_secrets FOR INSERT TO api_runtime
    WITH CHECK (bookings.actor_may_reveal(booking_id));
CREATE POLICY booking_secrets_update ON bookings.booking_secrets FOR UPDATE TO api_runtime
    USING (bookings.actor_may_reveal(booking_id))
    WITH CHECK (bookings.actor_may_reveal(booking_id));
GRANT SELECT, INSERT, UPDATE ON bookings.booking_secrets TO api_runtime;
"""

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
    DELETE FROM decisions.poll_outcomes WHERE plan_id = target;
    DELETE FROM decisions.poll_results WHERE plan_id = target;
    DELETE FROM decisions.poll_votes WHERE plan_id = target;
    DELETE FROM decisions.poll_electorate WHERE plan_id = target;
    DELETE FROM decisions.poll_options WHERE plan_id = target;
    DELETE FROM decisions.polls WHERE plan_id = target;
    DELETE FROM schedule_places.item_attendance WHERE plan_id = target;
    DELETE FROM schedule_places.itinerary_items WHERE plan_id = target;
    DELETE FROM bookings.booking_secrets WHERE plan_id = target;
    DELETE FROM bookings.bookings WHERE plan_id = target;
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


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(GUARDS_SQL)
    op.execute(RLS_SQL)
    op.execute(PURGE_SQL)


def downgrade() -> None:
    raise RuntimeError("Bookings are forward-only")
