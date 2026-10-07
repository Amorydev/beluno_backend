"""Trip planning: tasks and packing lists.

Revision ID: 000014_coordination
Revises: 000013_bookings
Create Date: 2026-10-07

Forward action:

* In the ``coordination`` schema (created empty by the platform migration):
  ``tasks`` (title, note, one assignee, a due date with an optional local time and
  zone, a reminder intent, status ``open``/``in_progress``/``done``, a link to an
  itinerary item or a booking), ``packing_items`` (``shared`` or ``private``;
  name, category, quantity, who brings a shared one, packed), and
  ``template_applications`` (a packing template applied once per plan, owner, and
  template).
* RLS: the plan's active participants read and write; private packing items are
  visible to and writable by their owner only (owners keep reading them after
  leaving the trip). Nobody deletes (tombstones).
  Guards: identities fixed and ``version + 1``; a task's content changes only by
  its creator or an organiser, its status also by its assignee; a shared packing
  item's content changes only by its creator or an organiser while anyone marks it
  packed; a private item stays its owner's and only ever becomes shared. An
  assignee whose participant row was merged into the actor's counts as the actor.
* ``coordination.transfer_private_packing`` moves a claiming guest's private list
  to the account; ``coordination.forget_private_packing`` removes a deleted
  account's private lists.
* ``plans.purge_deleted_plan`` also removes the plan's tasks and packing items
  (before its items and bookings) and tells owners' devices their private items
  are gone.

Lock/scan risk: new tables; their foreign keys take brief SHARE ROW EXCLUSIVE locks on
``plans.plans``, ``plans.plan_participants``, ``iam.users``,
``schedule_places.itinerary_items``, and ``bookings.bookings``; the purge function is
replaced.

Validation:
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'coordination' AND c.relkind = 'r' AND c.relrowsecurity;  -- 3

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000014_coordination"
down_revision = "000013_bookings"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE coordination.tasks (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    title text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
    note text CHECK (char_length(note) <= 2000),
    assignee_participant_id uuid,
    due_date date,
    due_time time,
    due_timezone text CHECK (char_length(due_timezone) BETWEEN 1 AND 64),
    remind_at timestamptz,
    status text NOT NULL CHECK (status IN ('open', 'in_progress', 'done')),
    completed_at timestamptz,
    completed_by_user_id uuid REFERENCES iam.users (id),
    item_id uuid,
    booking_id uuid,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, assignee_participant_id)
        REFERENCES plans.plan_participants (plan_id, id),
    FOREIGN KEY (plan_id, item_id) REFERENCES schedule_places.itinerary_items (plan_id, id),
    FOREIGN KEY (plan_id, booking_id) REFERENCES bookings.bookings (plan_id, id),
    CHECK (item_id IS NULL OR booking_id IS NULL),
    CHECK ((due_time IS NULL) = (due_timezone IS NULL)),
    CHECK (due_time IS NULL OR due_date IS NOT NULL),
    CHECK ((status = 'done') = (completed_at IS NOT NULL)),
    CHECK ((status = 'done') = (completed_by_user_id IS NOT NULL))
);
CREATE INDEX tasks_plan_idx ON coordination.tasks (plan_id, id);

CREATE TABLE coordination.packing_items (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    visibility text NOT NULL CHECK (visibility IN ('shared', 'private')),
    owner_user_id uuid REFERENCES iam.users (id),
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    category text NOT NULL CHECK (
        category IN ('clothes', 'toiletries', 'documents', 'electronics', 'gear', 'food',
                     'health', 'other')
    ),
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 99),
    bringer_participant_id uuid,
    packed boolean NOT NULL,
    template_id text CHECK (char_length(template_id) BETWEEN 1 AND 64),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    FOREIGN KEY (plan_id, bringer_participant_id)
        REFERENCES plans.plan_participants (plan_id, id),
    CHECK ((visibility = 'private') = (owner_user_id IS NOT NULL)),
    CHECK (visibility = 'shared' OR bringer_participant_id IS NULL)
);
CREATE INDEX packing_items_plan_idx ON coordination.packing_items (plan_id, id);
CREATE INDEX packing_items_owner_idx ON coordination.packing_items (owner_user_id, id)
    WHERE owner_user_id IS NOT NULL;

CREATE TABLE coordination.template_applications (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    owner_user_id uuid REFERENCES iam.users (id),
    template_id text NOT NULL CHECK (char_length(template_id) BETWEEN 1 AND 64),
    applied_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    applied_at timestamptz NOT NULL,
    UNIQUE NULLS NOT DISTINCT (plan_id, owner_user_id, template_id)
);
"""

GUARDS_SQL = """
CREATE FUNCTION coordination.actor_manages(p_plan_id uuid, p_creator uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND user_id = iam.actor_id() AND access_state = 'active'
          AND (role IN ('owner', 'admin') OR p_creator = iam.actor_id()
               OR p_creator IN (SELECT id FROM iam.users
                                WHERE merged_into_user_id = iam.actor_id()))
    )
$$;
REVOKE EXECUTE ON FUNCTION coordination.actor_manages(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coordination.actor_manages(uuid, uuid) TO api_runtime;

-- The actor is the task's assignee: their active row, or a row merged into it.
CREATE FUNCTION coordination.actor_is_assignee(p_plan_id uuid, p_assignee uuid)
    RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants AS p
        WHERE p.plan_id = p_plan_id AND p.user_id = iam.actor_id()
          AND p.access_state = 'active'
          AND (p.id = p_assignee
               OR EXISTS (SELECT 1 FROM plans.plan_participants AS m
                          WHERE m.plan_id = p_plan_id AND m.id = p_assignee
                            AND m.merged_into_participant_id = p.id))
    )
$$;
REVOKE EXECUTE ON FUNCTION coordination.actor_is_assignee(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coordination.actor_is_assignee(uuid, uuid) TO api_runtime;

CREATE FUNCTION coordination.guard_task() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NEW.deleted_at IS NOT NULL
           OR NEW.created_by_user_id IS DISTINCT FROM iam.actor_id() THEN
            RAISE EXCEPTION 'a task is added in its creator''s name'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.plan_id, NEW.created_by_user_id, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.plan_id, OLD.created_by_user_id, OLD.created_at)
       OR OLD.deleted_at IS NOT NULL OR NEW.version <> OLD.version + 1
       -- Whoever completes a task is recorded as themselves.
       OR (NEW.completed_by_user_id IS DISTINCT FROM OLD.completed_by_user_id
           AND NEW.completed_by_user_id IS DISTINCT FROM iam.actor_id()
           AND NEW.completed_by_user_id IS NOT NULL) THEN
        RAISE EXCEPTION 'coordination.tasks does not allow this change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF coordination.actor_manages(OLD.plan_id, OLD.created_by_user_id) THEN
        RETURN NEW;
    END IF;
    -- The assignee moves the status, and nothing else.
    IF to_jsonb(NEW) - 'status' - 'completed_at' - 'completed_by_user_id' - 'version'
           - 'updated_at'
       IS DISTINCT FROM to_jsonb(OLD) - 'status' - 'completed_at' - 'completed_by_user_id'
           - 'version' - 'updated_at'
       OR NOT coordination.actor_is_assignee(OLD.plan_id, OLD.assignee_participant_id) THEN
        RAISE EXCEPTION 'only the creator or an organiser changes this task'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION coordination.guard_packing() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NEW.deleted_at IS NOT NULL
           OR NEW.created_by_user_id IS DISTINCT FROM iam.actor_id()
           OR (NEW.visibility = 'private' AND NEW.owner_user_id IS DISTINCT FROM iam.actor_id())
        THEN
            RAISE EXCEPTION 'a packing item is added in its creator''s name'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.plan_id, NEW.created_by_user_id, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.plan_id, OLD.created_by_user_id, OLD.created_at)
       OR OLD.deleted_at IS NOT NULL OR NEW.version <> OLD.version + 1
       OR (OLD.visibility = 'shared' AND NEW.visibility <> 'shared') THEN
        RAISE EXCEPTION 'coordination.packing_items does not allow this change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.visibility = 'private' THEN
        -- A private item is its owner's (who may share it, never someone else).
        IF OLD.owner_user_id IS DISTINCT FROM iam.actor_id() THEN
            RAISE EXCEPTION 'a private item is its owner''s'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    -- Shared: anyone marks it packed; its content is its creator's or an organiser's.
    IF to_jsonb(NEW) - 'packed' - 'version' - 'updated_at'
       IS DISTINCT FROM to_jsonb(OLD) - 'packed' - 'version' - 'updated_at'
       AND NOT coordination.actor_manages(OLD.plan_id, OLD.created_by_user_id) THEN
        RAISE EXCEPTION 'only the creator or an organiser changes this item'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_guard BEFORE INSERT OR UPDATE ON coordination.tasks
    FOR EACH ROW EXECUTE FUNCTION coordination.guard_task();
CREATE TRIGGER packing_items_guard BEFORE INSERT OR UPDATE ON coordination.packing_items
    FOR EACH ROW EXECUTE FUNCTION coordination.guard_packing();
REVOKE EXECUTE ON FUNCTION coordination.guard_task(), coordination.guard_packing() FROM PUBLIC;
"""

RLS_SQL = """
ALTER TABLE coordination.tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tasks_select ON coordination.tasks FOR SELECT TO api_runtime
    USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY tasks_insert ON coordination.tasks FOR INSERT TO api_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY tasks_update ON coordination.tasks FOR UPDATE TO api_runtime
    USING (plans.actor_is_active_participant(plan_id))
    WITH CHECK (plans.actor_is_active_participant(plan_id));
GRANT SELECT, INSERT, UPDATE ON coordination.tasks TO api_runtime;

ALTER TABLE coordination.packing_items ENABLE ROW LEVEL SECURITY;
-- Shared items have no owner (a CHECK ties the owner to private visibility).
-- Owners always read their own private items, also after leaving the trip: nothing
-- tells their devices to drop them then, so a snapshot must still show them.
CREATE POLICY packing_items_select ON coordination.packing_items FOR SELECT TO api_runtime
    USING (owner_user_id = iam.actor_id()
           OR (owner_user_id IS NULL AND plans.actor_is_active_participant(plan_id)));
CREATE POLICY packing_items_insert ON coordination.packing_items FOR INSERT TO api_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id)
                AND (owner_user_id IS NULL OR owner_user_id = iam.actor_id()));
CREATE POLICY packing_items_update ON coordination.packing_items FOR UPDATE TO api_runtime
    USING (plans.actor_is_active_participant(plan_id)
           AND (owner_user_id IS NULL OR owner_user_id = iam.actor_id()))
    WITH CHECK (plans.actor_is_active_participant(plan_id)
                AND (owner_user_id IS NULL OR owner_user_id = iam.actor_id()));
GRANT SELECT, INSERT, UPDATE ON coordination.packing_items TO api_runtime;

ALTER TABLE coordination.template_applications ENABLE ROW LEVEL SECURITY;
CREATE POLICY template_applications_select ON coordination.template_applications
    FOR SELECT TO api_runtime
    USING (plans.actor_is_active_participant(plan_id)
           AND (owner_user_id IS NULL OR owner_user_id = iam.actor_id()));
CREATE POLICY template_applications_insert ON coordination.template_applications
    FOR INSERT TO api_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id)
                AND (owner_user_id IS NULL OR owner_user_id = iam.actor_id())
                AND applied_by_user_id = iam.actor_id());
GRANT SELECT, INSERT ON coordination.template_applications TO api_runtime;
"""

ACCOUNT_SQL = """
-- A guest who signs in to an existing account brings their private packing list:
-- items (and template applications) move to the account in every plan the account
-- now holds the guest's participation in. Returns the moved items.
CREATE FUNCTION coordination.transfer_private_packing(p_target uuid)
    RETURNS TABLE (item_id uuid, plan_id uuid, item_version integer)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    guest uuid := iam.actor_id();
BEGIN
    IF guest IS NULL OR p_target IS NULL OR p_target = guest THEN
        RETURN;
    END IF;
    -- Where the account already applied the same template, its application stands.
    DELETE FROM coordination.template_applications AS g
    WHERE g.owner_user_id = guest
      AND EXISTS (SELECT 1 FROM coordination.template_applications AS t
                  WHERE t.plan_id = g.plan_id AND t.owner_user_id = p_target
                    AND t.template_id = g.template_id);
    UPDATE coordination.template_applications AS g SET owner_user_id = p_target
    WHERE g.owner_user_id = guest
      AND EXISTS (SELECT 1 FROM plans.plan_participants AS p
                  WHERE p.plan_id = g.plan_id AND p.user_id = p_target
                    AND p.access_state <> 'merged');
    RETURN QUERY
    UPDATE coordination.packing_items AS i
    SET owner_user_id = p_target, version = i.version + 1, updated_at = now()
    WHERE i.owner_user_id = guest
      AND EXISTS (SELECT 1 FROM plans.plan_participants AS p
                  WHERE p.plan_id = i.plan_id AND p.user_id = p_target
                    AND p.access_state <> 'merged')
    RETURNING i.id, i.plan_id, i.version;
END;
$$;
REVOKE EXECUTE ON FUNCTION coordination.transfer_private_packing(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coordination.transfer_private_packing(uuid) TO api_runtime;

-- Deleting an account removes its private packing lists for good (nobody else can
-- read them, and they may name health items).
CREATE FUNCTION coordination.forget_private_packing() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    removed integer;
BEGIN
    IF actor IS NULL THEN
        RETURN 0;
    END IF;
    DELETE FROM coordination.template_applications WHERE owner_user_id = actor;
    DELETE FROM coordination.packing_items WHERE owner_user_id = actor;
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;
REVOKE EXECUTE ON FUNCTION coordination.forget_private_packing() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coordination.forget_private_packing() TO api_runtime;
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

    -- Private packing items (those with an owner) live in their owners' user scopes:
    -- tell those devices too.
    PERFORM sync_audit.append_changes(coalesce((
        SELECT jsonb_agg(jsonb_build_object(
                   'changed_at', now_at,
                   'scope_type', 'user',
                   'scope_id', i.owner_user_id,
                   'entity_type', 'packing_item',
                   'entity_id', i.id,
                   'entity_version', i.version + 1,
                   'operation', 'delete'
               ) ORDER BY i.id)
        FROM coordination.packing_items AS i
        WHERE i.plan_id = target AND i.owner_user_id IS NOT NULL AND i.deleted_at IS NULL
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
    DELETE FROM coordination.template_applications WHERE plan_id = target;
    DELETE FROM coordination.packing_items WHERE plan_id = target;
    DELETE FROM coordination.tasks WHERE plan_id = target;
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
    op.execute(ACCOUNT_SQL)
    op.execute(PURGE_SQL)


def downgrade() -> None:
    raise RuntimeError("Tasks and packing lists are forward-only")
