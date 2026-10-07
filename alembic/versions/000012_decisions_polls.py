"""Trip planning: polls.

Revision ID: 000012_decisions_polls
Revises: 000011_planning_places
Create Date: 2026-10-07

Forward action:

* In the ``decisions`` schema (created empty by the platform migration):
  ``polls`` (single choice, or yes/no with an optional quorum; an optional
  deadline; whether votes may change), ``poll_options`` (label, optional saved
  place, position; yes/no options carry ``answer``), ``poll_electorate`` (the
  participants active when the poll opened), ``poll_votes`` (one per voter,
  updated in place), ``poll_results`` (one immutable result per poll), and
  ``poll_outcomes`` (what was done with a result, one per poll and action).
* RLS: the plan's active participants read; they insert polls, options, votes,
  and outcomes and update polls and votes. Nobody deletes (polls are tombstoned)
  and only the database writes the electorate and results. Guards:
  - a poll opens open, at version 1, in its creator's name; afterwards only its
    version moves, and whoever manages it (creator or organiser) may withdraw it
    while open (questions, options, deadlines, and rules stay as opened);
  - options come from the creator before the poll opens;
  - ``decisions.open_poll`` snapshots the electorate once (active participants,
    placeholders excluded), which seals the options;
  - a vote is the voter's own, from the electorate (foreign key), on an option of
    that poll, while it is open and before its deadline (and stays put when changes
    are not allowed);
  - an outcome acts on the decided result (its winner, or one tied option shared by
    every action of the poll) and is applied by whoever manages the poll.
* Yes/no polls pass when yes outnumbers no and reaches the quorum (if any); with no
  votes at all a poll closes as ``no_votes``.
* Closing has one path: ``decisions.finalize_poll`` (not callable directly)
  locks the poll, returns at once when it is already closed, otherwise writes
  the result, closes the poll, and appends its change rows, the ``poll.closed``
  feed event, and an audit row. ``decisions.close_poll`` (API: the creator or an
  organiser) and ``decisions.close_due_poll`` (worker: a poll past its deadline,
  skipping locked ones) call it, so a job racing an organiser yields one result.
* ``plans.purge_deleted_plan`` also removes the plan's polls (before its places).

Lock/scan risk: new tables; their foreign keys take brief SHARE ROW EXCLUSIVE locks
on ``plans.plans``, ``plans.plan_participants``, ``schedule_places.places``, and
``iam.users``; the purge function is replaced (no table lock).

Validation:
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'decisions' AND c.relkind = 'r' AND c.relrowsecurity;     -- 6
    SELECT has_table_privilege('api_runtime', 'decisions.poll_electorate', 'INSERT'); -- f
    SELECT has_function_privilege('api_runtime',
        'decisions.finalize_poll(uuid, uuid, uuid, uuid)', 'EXECUTE');           -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000012_decisions_polls"
down_revision = "000011_planning_places"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE decisions.polls (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    kind text NOT NULL CHECK (kind IN ('single_choice', 'yes_no')),
    question text NOT NULL CHECK (char_length(btrim(question)) BETWEEN 1 AND 200),
    deadline_at timestamptz,
    quorum integer CHECK (quorum BETWEEN 1 AND 100),
    allow_vote_change boolean NOT NULL,
    status text NOT NULL CHECK (status IN ('open', 'closed')),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    closed_at timestamptz,
    closed_by_user_id uuid REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    CHECK (kind = 'yes_no' OR quorum IS NULL),
    CHECK ((status = 'closed') = (closed_at IS NOT NULL))
);
CREATE INDEX polls_due_idx ON decisions.polls (deadline_at)
    WHERE status = 'open' AND deleted_at IS NULL AND deadline_at IS NOT NULL;

CREATE TABLE decisions.poll_options (
    id uuid PRIMARY KEY,
    poll_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    label text NOT NULL CHECK (char_length(btrim(label)) BETWEEN 1 AND 120),
    place_id uuid,
    answer text CHECK (answer IN ('yes', 'no')),
    position integer NOT NULL CHECK (position BETWEEN 0 AND 19),
    UNIQUE (poll_id, position),
    UNIQUE (poll_id, id),
    FOREIGN KEY (plan_id, poll_id) REFERENCES decisions.polls (plan_id, id),
    FOREIGN KEY (plan_id, place_id) REFERENCES schedule_places.places (plan_id, id)
);
CREATE INDEX poll_options_plan_idx ON decisions.poll_options (plan_id, poll_id);

CREATE TABLE decisions.poll_electorate (
    poll_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    participant_id uuid NOT NULL,
    PRIMARY KEY (poll_id, participant_id),
    FOREIGN KEY (plan_id, poll_id) REFERENCES decisions.polls (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);
CREATE INDEX poll_electorate_plan_idx ON decisions.poll_electorate (plan_id, participant_id);

CREATE TABLE decisions.poll_votes (
    poll_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    participant_id uuid NOT NULL,
    option_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (poll_id, participant_id),
    FOREIGN KEY (poll_id, option_id) REFERENCES decisions.poll_options (poll_id, id),
    FOREIGN KEY (poll_id, participant_id)
        REFERENCES decisions.poll_electorate (poll_id, participant_id)
);
CREATE INDEX poll_votes_plan_idx ON decisions.poll_votes (plan_id, poll_id);

CREATE TABLE decisions.poll_results (
    poll_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL,
    version integer NOT NULL CHECK (version = 1),
    outcome text NOT NULL CHECK (outcome IN ('winner', 'tie', 'no_votes', 'passed', 'failed')),
    winner_option_id uuid,
    tied_option_ids uuid[] NOT NULL,
    counts jsonb NOT NULL CHECK (jsonb_typeof(counts) = 'object'),
    eligible integer NOT NULL CHECK (eligible >= 0),
    voted integer NOT NULL CHECK (voted >= 0),
    closed_at timestamptz NOT NULL,
    closed_by_user_id uuid REFERENCES iam.users (id),
    FOREIGN KEY (plan_id, poll_id) REFERENCES decisions.polls (plan_id, id),
    CHECK ((outcome = 'winner') = (winner_option_id IS NOT NULL))
);
CREATE INDEX poll_results_plan_idx ON decisions.poll_results (plan_id);

CREATE TABLE decisions.poll_outcomes (
    poll_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('save_place', 'add_to_plan')),
    plan_id uuid NOT NULL,
    result_version integer NOT NULL CHECK (result_version = 1),
    option_id uuid NOT NULL,
    created_entity_id uuid NOT NULL,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (poll_id, action),
    FOREIGN KEY (plan_id, poll_id) REFERENCES decisions.polls (plan_id, id),
    FOREIGN KEY (poll_id, option_id) REFERENCES decisions.poll_options (poll_id, id)
);
CREATE INDEX poll_outcomes_plan_idx ON decisions.poll_outcomes (plan_id);
"""

GUARDS_SQL = """
-- Whether the actor may manage the poll: its creator (or a guest account merged into
-- the actor) or an active owner/admin of the plan.
CREATE FUNCTION decisions.actor_manages_poll(p_plan_id uuid, p_creator uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND user_id = iam.actor_id() AND access_state = 'active'
          AND (role IN ('owner', 'admin') OR p_creator = iam.actor_id()
               OR p_creator IN (SELECT id FROM iam.users
                                WHERE merged_into_user_id = iam.actor_id()))
    )
$$;

CREATE FUNCTION decisions.guard_poll() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        -- A poll starts open, unclosed, at version 1, in its creator's name.
        IF NEW.status <> 'open' OR NEW.closed_at IS NOT NULL OR NEW.closed_by_user_id IS NOT NULL
           OR NEW.version <> 1 OR NEW.deleted_at IS NOT NULL
           OR NEW.created_by_user_id IS DISTINCT FROM iam.actor_id() THEN
            RAISE EXCEPTION 'a poll opens in its creator''s name'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    -- After that only its version moves (votes, outcomes), and it may be withdrawn
    -- while open by whoever manages it. Questions, options, deadlines, and rules stay
    -- as opened; closing happens only through the database's close path.
    IF OLD.deleted_at IS NOT NULL OR NEW.version <> OLD.version + 1
       OR to_jsonb(NEW) - 'version' - 'updated_at' - 'deleted_at'
          IS DISTINCT FROM to_jsonb(OLD) - 'version' - 'updated_at' - 'deleted_at'
       OR (NEW.deleted_at IS NOT NULL AND (
           OLD.status <> 'open'
           OR NOT decisions.actor_manages_poll(OLD.plan_id, OLD.created_by_user_id)
       )) THEN
        RAISE EXCEPTION 'decisions.polls does not allow this change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

-- Options are written by the poll's creator before it opens (no electorate yet).
CREATE FUNCTION decisions.guard_option() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM decisions.polls
        WHERE id = NEW.poll_id AND plan_id = NEW.plan_id AND version = 1 AND status = 'open'
          AND created_by_user_id = iam.actor_id()
    ) OR EXISTS (SELECT 1 FROM decisions.poll_electorate WHERE poll_id = NEW.poll_id) THEN
        RAISE EXCEPTION 'options are set before the poll opens'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION decisions.guard_vote() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    poll decisions.polls%ROWTYPE;
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    SELECT * INTO poll FROM decisions.polls WHERE id = NEW.poll_id;
    IF TG_OP = 'UPDATE' AND (
        NEW.poll_id <> OLD.poll_id OR NEW.plan_id <> OLD.plan_id
        OR NEW.participant_id <> OLD.participant_id OR NEW.created_at <> OLD.created_at
        OR (NEW.option_id <> OLD.option_id AND NOT poll.allow_vote_change)
    ) THEN
        RAISE EXCEPTION 'decisions.poll_votes does not allow this change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF poll.plan_id <> NEW.plan_id OR poll.status <> 'open' OR poll.deleted_at IS NOT NULL
       OR (poll.deadline_at IS NOT NULL AND poll.deadline_at <= transaction_timestamp())
       OR NOT EXISTS (
           SELECT 1 FROM plans.plan_participants
           WHERE plan_id = NEW.plan_id AND id = NEW.participant_id AND user_id = iam.actor_id()
       ) THEN
        RAISE EXCEPTION 'votes are cast by the voter, on an open poll'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

-- An outcome acts on the poll's decided result: its winner, or one tied option that
-- every action of that poll then shares; only those who manage the poll apply it.
CREATE FUNCTION decisions.guard_outcome() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    result decisions.poll_results%ROWTYPE;
    creator uuid;
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    SELECT r.* INTO result FROM decisions.poll_results AS r WHERE r.poll_id = NEW.poll_id;
    SELECT created_by_user_id INTO creator FROM decisions.polls WHERE id = NEW.poll_id;
    IF result.poll_id IS NULL OR result.plan_id <> NEW.plan_id
       OR NEW.result_version <> result.version
       OR NEW.created_by_user_id IS DISTINCT FROM iam.actor_id()
       OR NOT decisions.actor_manages_poll(NEW.plan_id, creator)
       OR NOT (
           (result.outcome = 'winner' AND NEW.option_id = result.winner_option_id)
           OR (result.outcome = 'tie' AND NEW.option_id = ANY (result.tied_option_ids))
       )
       OR EXISTS (
           SELECT 1 FROM decisions.poll_outcomes
           WHERE poll_id = NEW.poll_id AND option_id <> NEW.option_id
       ) THEN
        RAISE EXCEPTION 'outcomes act on the poll''s decided result'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER polls_guard BEFORE INSERT OR UPDATE ON decisions.polls
    FOR EACH ROW EXECUTE FUNCTION decisions.guard_poll();
CREATE TRIGGER poll_options_guard BEFORE INSERT ON decisions.poll_options
    FOR EACH ROW EXECUTE FUNCTION decisions.guard_option();
CREATE TRIGGER poll_votes_guard BEFORE INSERT OR UPDATE ON decisions.poll_votes
    FOR EACH ROW EXECUTE FUNCTION decisions.guard_vote();
CREATE TRIGGER poll_outcomes_guard BEFORE INSERT ON decisions.poll_outcomes
    FOR EACH ROW EXECUTE FUNCTION decisions.guard_outcome();
REVOKE EXECUTE ON FUNCTION decisions.guard_poll(), decisions.guard_option(),
    decisions.guard_vote(), decisions.guard_outcome() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION decisions.actor_manages_poll(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decisions.actor_manages_poll(uuid, uuid) TO api_runtime;

-- Opening snapshots the electorate (everyone active who can answer; placeholders are
-- names, not people) once, for the poll's creator. Options are sealed from then on.
CREATE FUNCTION decisions.open_poll(p_poll_id uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    poll decisions.polls%ROWTYPE;
    opened integer;
BEGIN
    SELECT * INTO poll FROM decisions.polls WHERE id = p_poll_id FOR UPDATE;
    IF NOT FOUND OR poll.created_by_user_id IS DISTINCT FROM iam.actor_id()
       OR poll.version <> 1 OR poll.status <> 'open' OR poll.deleted_at IS NOT NULL
       OR NOT plans.actor_is_active_participant(poll.plan_id)
       OR EXISTS (SELECT 1 FROM decisions.poll_electorate WHERE poll_id = poll.id)
       OR (SELECT count(*) FROM decisions.poll_options WHERE poll_id = poll.id) < 2 THEN
        RAISE EXCEPTION 'only a new poll, by its creator, opens once'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO decisions.poll_electorate (poll_id, plan_id, participant_id)
    SELECT poll.id, poll.plan_id, p.id
    FROM plans.plan_participants AS p
    WHERE p.plan_id = poll.plan_id AND p.access_state = 'active'
      AND p.identity_kind <> 'placeholder';
    GET DIAGNOSTICS opened = ROW_COUNT;
    RETURN opened;
END;
$$;
REVOKE EXECUTE ON FUNCTION decisions.open_poll(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decisions.open_poll(uuid) TO api_runtime;
"""

READ_ONLY = ("poll_results", "poll_electorate")
INSERT_ONLY = ("poll_options", "poll_outcomes")
MUTABLE = ("polls", "poll_votes")

RLS_SQL = (
    "\n".join(
        f"ALTER TABLE decisions.{table} ENABLE ROW LEVEL SECURITY;\n"
        f"CREATE POLICY {table}_select ON decisions.{table} FOR SELECT TO api_runtime\n"
        f"    USING (plans.actor_is_active_participant(plan_id));\n"
        f"GRANT SELECT ON decisions.{table} TO api_runtime;"
        for table in READ_ONLY + INSERT_ONLY + MUTABLE
    )
    + "\n"
    + "\n".join(
        f"CREATE POLICY {table}_insert ON decisions.{table} FOR INSERT TO api_runtime\n"
        f"    WITH CHECK (plans.actor_is_active_participant(plan_id));\n"
        f"GRANT INSERT ON decisions.{table} TO api_runtime;"
        for table in INSERT_ONLY + MUTABLE
    )
    + "\n"
    + "\n".join(
        f"CREATE POLICY {table}_update ON decisions.{table} FOR UPDATE TO api_runtime\n"
        f"    USING (plans.actor_is_active_participant(plan_id))\n"
        f"    WITH CHECK (plans.actor_is_active_participant(plan_id));\n"
        f"GRANT UPDATE ON decisions.{table} TO api_runtime;"
        for table in MUTABLE
    )
)

CLOSE_SQL = """
-- The one way a poll closes. Event and audit IDs come from the caller (UUIDv7, so
-- the feed keeps its order); NULL ``p_closed_by`` means the deadline closed it.
CREATE FUNCTION decisions.finalize_poll(
    p_poll_id uuid, p_closed_by uuid, p_event_id uuid, p_audit_id uuid
) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    poll decisions.polls%ROWTYPE;
    now_at timestamptz := transaction_timestamp();
    tally jsonb;
    eligible integer;
    voted integer;
    top integer;
    leaders uuid[];
    yes_votes integer;
    no_votes integer;
    result_outcome text;
    winner uuid;
    summary jsonb;
BEGIN
    SELECT * INTO poll FROM decisions.polls WHERE id = p_poll_id FOR UPDATE;
    IF NOT FOUND OR poll.deleted_at IS NOT NULL THEN
        RETURN NULL;
    END IF;
    IF poll.status = 'closed' THEN
        RETURN poll.id;
    END IF;
    SELECT count(*) INTO eligible FROM decisions.poll_electorate WHERE poll_id = poll.id;
    SELECT count(*) INTO voted FROM decisions.poll_votes WHERE poll_id = poll.id;
    SELECT coalesce(jsonb_object_agg(o.id::text, coalesce(v.n, 0)), '{}'::jsonb),
           coalesce(max(coalesce(v.n, 0)), 0)
    INTO tally, top
    FROM decisions.poll_options AS o
    LEFT JOIN (
        SELECT option_id, count(*)::integer AS n FROM decisions.poll_votes
        WHERE poll_id = poll.id GROUP BY option_id
    ) AS v ON v.option_id = o.id
    WHERE o.poll_id = poll.id;
    leaders := '{}';
    IF poll.kind = 'yes_no' THEN
        SELECT count(*) FILTER (WHERE o.answer = 'yes'), count(*) FILTER (WHERE o.answer = 'no')
        INTO yes_votes, no_votes
        FROM decisions.poll_votes AS v JOIN decisions.poll_options AS o ON o.id = v.option_id
        WHERE v.poll_id = poll.id;
        -- Yes must outnumber no, and reach the quorum when there is one.
        result_outcome := CASE
            WHEN voted = 0 THEN 'no_votes'
            WHEN yes_votes > no_votes AND yes_votes >= coalesce(poll.quorum, 0) THEN 'passed'
            ELSE 'failed' END;
    ELSIF voted = 0 THEN
        result_outcome := 'no_votes';
    ELSE
        SELECT array_agg(o.id ORDER BY o.position) INTO leaders
        FROM decisions.poll_options AS o
        WHERE o.poll_id = poll.id AND (tally ->> o.id::text)::integer = top;
        IF cardinality(leaders) = 1 THEN
            result_outcome := 'winner';
            winner := leaders[1];
            leaders := '{}';
        ELSE
            result_outcome := 'tie';
        END IF;
    END IF;
    INSERT INTO decisions.poll_results (
        poll_id, plan_id, version, outcome, winner_option_id, tied_option_ids, counts,
        eligible, voted, closed_at, closed_by_user_id
    ) VALUES (
        poll.id, poll.plan_id, 1, result_outcome, winner, leaders, tally,
        eligible, voted, now_at, p_closed_by
    );
    UPDATE decisions.polls
    SET status = 'closed', closed_at = now_at, closed_by_user_id = p_closed_by,
        version = version + 1, updated_at = now_at
    WHERE id = poll.id;
    summary := jsonb_strip_nulls(jsonb_build_object('outcome', result_outcome,
                                                     'option_id', winner::text));
    INSERT INTO activity.events (
        id, scope_type, scope_id, plan_id, actor_user_id, type, entity_type, entity_id,
        summary, occurred_at
    ) VALUES (
        p_event_id, 'plan', poll.plan_id, poll.plan_id, p_closed_by, 'poll.closed', 'poll',
        poll.id, summary, now_at
    );
    PERFORM sync_audit.append_changes(jsonb_build_array(
        jsonb_build_object(
            'changed_at', now_at, 'scope_type', 'plan', 'scope_id', poll.plan_id,
            'entity_type', 'poll', 'entity_id', poll.id, 'entity_version', poll.version + 1,
            'operation', 'upsert', 'actor_user_id', p_closed_by
        ),
        jsonb_build_object(
            'changed_at', now_at, 'scope_type', 'plan', 'scope_id', poll.plan_id,
            'entity_type', 'activity_event', 'entity_id', p_event_id, 'entity_version', 1,
            'operation', 'upsert', 'actor_user_id', p_closed_by
        )
    ));
    INSERT INTO sync_audit.audit_events (
        id, occurred_at, actor_user_id, action, entity_type, entity_id, plan_id, metadata
    ) VALUES (
        p_audit_id, now_at, p_closed_by, 'planning.poll_closed', 'poll', poll.id,
        poll.plan_id, jsonb_build_object('outcome', result_outcome)
    );
    RETURN poll.id;
END;
$$;

-- An organiser (or the poll's creator) closes it early.
CREATE FUNCTION decisions.close_poll(p_poll_id uuid, p_event_id uuid, p_audit_id uuid)
    RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    poll_plan uuid;
    poll_creator uuid;
BEGIN
    SELECT plan_id, created_by_user_id INTO poll_plan, poll_creator
    FROM decisions.polls WHERE id = p_poll_id;
    IF actor IS NULL OR poll_plan IS NULL OR NOT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = poll_plan AND user_id = actor AND access_state = 'active'
          AND (role IN ('owner', 'admin') OR poll_creator = actor
               OR poll_creator IN (SELECT id FROM iam.users WHERE merged_into_user_id = actor))
    ) THEN
        RAISE EXCEPTION 'only the poll''s creator or an organiser closes it'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN decisions.finalize_poll(p_poll_id, actor, p_event_id, p_audit_id);
END;
$$;

-- The worker closes one poll whose deadline has passed (another run takes the next).
CREATE FUNCTION decisions.close_due_poll(p_event_id uuid, p_audit_id uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    due uuid;
BEGIN
    SELECT id INTO due FROM decisions.polls
    WHERE status = 'open' AND deleted_at IS NULL AND deadline_at <= transaction_timestamp()
    ORDER BY deadline_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED;
    IF due IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN decisions.finalize_poll(due, NULL, p_event_id, p_audit_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION decisions.finalize_poll(uuid, uuid, uuid, uuid),
    decisions.close_poll(uuid, uuid, uuid), decisions.close_due_poll(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decisions.close_poll(uuid, uuid, uuid) TO api_runtime;
GRANT EXECUTE ON FUNCTION decisions.close_due_poll(uuid, uuid) TO worker_runtime;
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
    op.execute(CLOSE_SQL)
    op.execute(PURGE_SQL)


def downgrade() -> None:
    raise RuntimeError("Polls are forward-only")
