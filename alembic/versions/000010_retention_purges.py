"""Purge deleted plans, stale sessions, and the names of deleted crews.

Revision ID: 000010_retention_purges
Revises: 000009_activity_and_deletion
Create Date: 2026-10-07

Forward action:

* ``finance.reject_history_change`` keeps finance history append-only, with one
  exception: a DELETE by a role that is not a guarded runtime while the
  transaction-local flag ``beluno.plan_purge`` is on. Only the purge gate below
  raises that flag (as the table owner) and clears it before returning; runtime
  roles also hold no DELETE grant on finance tables.
* ``plans.purge_deleted_plan(p_cutoff)`` (SECURITY DEFINER, worker) takes one plan
  whose deletion was scheduled at or before the cutoff, skipping plans another
  transaction holds, and deletes it outright: finance rows, feed events,
  invites, participants, plan-scope change rows, and the scope head. Former
  participants get a ``plan_access`` delete in their user scope so their devices
  drop the plan. Audit events stay (they hold IDs, never text). Cutoffs newer
  than seven days ago are refused, so a misconfiguration cannot purge plans
  people are still able to restore.
* ``iam.purge_stale_sessions(p_cutoff)`` (SECURITY DEFINER, worker) deletes
  sessions revoked or expired before the cutoff, with their refresh tokens, so
  device labels do not outlive a session by long. The worker holds no grant on
  sessions; cutoffs newer than seven days ago are refused.
* A deleted crew keeps no name and no members: ``crews_name_check`` allows an
  empty name on tombstones, ``people.forget_member`` blanks the name of a crew it
  empties, and crews deleted earlier are cleared now.
* ``plans.guard_deletion_schedule``: in the guarded runtime only the plan's owner
  may schedule or cancel its deletion, and a schedule is stamped with the current
  time (never backdated), so nobody can bring a purge forward.
* Indexes on ``plan_id`` (and the participant it references) for the finance and
  feed tables that lacked one, and on ``plans.duplicated_from_plan_id``, so a
  purge and its foreign-key checks do not scan whole tables.

Lock/scan risk: replaces two functions (no table lock), adds three and a trigger,
replaces one check constraint on ``people.crews`` (brief ACCESS EXCLUSIVE lock, one
scan), and builds seven indexes without CONCURRENTLY (SHARE locks block writes to
those tables while they build; acceptable before launch, small tables).

Validation:
    SELECT has_function_privilege('worker_runtime', 'plans.purge_deleted_plan(timestamptz)',
                                  'EXECUTE');                                    -- t
    SELECT has_function_privilege('api_runtime', 'plans.purge_deleted_plan(timestamptz)',
                                  'EXECUTE');                                    -- f
    SELECT count(*) FROM people.crews WHERE deleted_at IS NULL AND name = '';    -- 0

Compatibility: additive; finance history stays append-only for every runtime role.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000010_retention_purges"
down_revision = "000009_activity_and_deletion"
branch_labels = None
depends_on = None

PURGE_SQL = """
CREATE OR REPLACE FUNCTION finance.reject_history_change() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    -- The plan purge gate deletes a whole plan once its restore window is over;
    -- nothing else ever removes or rewrites finance history.
    IF TG_OP = 'DELETE'
       AND NOT iam.is_guarded_runtime()
       AND current_setting('beluno.plan_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'finance.% rows are append-only', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE FUNCTION plans.purge_deleted_plan(p_cutoff timestamptz) RETURNS uuid
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
REVOKE EXECUTE ON FUNCTION plans.purge_deleted_plan(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION plans.purge_deleted_plan(timestamptz) TO worker_runtime;
"""

SESSIONS_SQL = """
CREATE FUNCTION iam.purge_stale_sessions(p_cutoff timestamptz) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    removed integer;
BEGIN
    IF p_cutoff IS NULL OR p_cutoff > transaction_timestamp() - interval '7 days' THEN
        RAISE EXCEPTION 'sessions are purged no sooner than seven days after they end'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- A session that ended before the cutoff can never come back (refresh needs a
    -- live session), so both statements see the same set.
    DELETE FROM iam.refresh_tokens AS t
    USING iam.sessions AS s
    WHERE t.session_id = s.id
      AND (s.revoked_at < p_cutoff OR s.absolute_expires_at < p_cutoff
           OR s.idle_expires_at < p_cutoff);
    DELETE FROM iam.sessions
    WHERE revoked_at < p_cutoff OR absolute_expires_at < p_cutoff OR idle_expires_at < p_cutoff;
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;
REVOKE EXECUTE ON FUNCTION iam.purge_stale_sessions(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.purge_stale_sessions(timestamptz) TO worker_runtime;
"""

CREWS_SQL = """
UPDATE people.crews SET name = '', member_user_ids = '{}'
WHERE deleted_at IS NOT NULL AND (name <> '' OR cardinality(member_user_ids) > 0);

ALTER TABLE people.crews
    DROP CONSTRAINT crews_name_check,
    ADD CONSTRAINT crews_name_check CHECK (
        char_length(btrim(name)) BETWEEN 1 AND 60 OR (deleted_at IS NOT NULL AND name = '')
    );

CREATE OR REPLACE FUNCTION people.forget_member()
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
            name = CASE
                WHEN cardinality(array_remove(c.member_user_ids, actor)) = 0
                THEN '' ELSE c.name END,
            version = c.version + 1,
            updated_at = transaction_timestamp()
        WHERE actor = ANY (c.member_user_ids)
          AND c.owner_user_id <> actor
          AND c.deleted_at IS NULL
        RETURNING c.id, c.owner_user_id, c.version, c.deleted_at IS NOT NULL;
END;
$$;
"""


SCHEDULE_SQL = """
CREATE FUNCTION plans.guard_deletion_schedule() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime()
       OR NEW.deletion_scheduled_at IS NOT DISTINCT FROM OLD.deletion_scheduled_at THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = NEW.id AND user_id = iam.actor_id()
          AND role = 'owner' AND access_state = 'active'
    ) THEN
        RAISE EXCEPTION 'only the owner schedules or cancels deletion'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- A schedule starts the restore window now; it is never moved once set.
    IF NEW.deletion_scheduled_at IS NOT NULL AND (
        OLD.deletion_scheduled_at IS NOT NULL
        OR abs(extract(epoch FROM NEW.deletion_scheduled_at - transaction_timestamp())) > 300
    ) THEN
        RAISE EXCEPTION 'deletion is scheduled at the current time only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE EXECUTE ON FUNCTION plans.guard_deletion_schedule() FROM PUBLIC;
CREATE TRIGGER plans_deletion_schedule_guard
    BEFORE UPDATE OF deletion_scheduled_at ON plans.plans
    FOR EACH ROW EXECUTE FUNCTION plans.guard_deletion_schedule();
"""

INDEXES_SQL = """
CREATE INDEX events_plan_idx ON activity.events (plan_id) WHERE plan_id IS NOT NULL;
CREATE INDEX plans_duplicated_from_idx ON plans.plans (duplicated_from_plan_id)
    WHERE duplicated_from_plan_id IS NOT NULL;
CREATE INDEX expense_payers_plan_participant_idx
    ON finance.expense_payers (plan_id, participant_id);
CREATE INDEX expense_splits_plan_participant_idx
    ON finance.expense_splits (plan_id, participant_id);
CREATE INDEX refund_shares_plan_participant_idx
    ON finance.refund_shares (plan_id, participant_id);
CREATE INDEX consolidation_lines_plan_participant_idx
    ON finance.consolidation_lines (plan_id, participant_id);
CREATE INDEX consolidation_rates_plan_idx ON finance.consolidation_rates (plan_id);
"""


def upgrade() -> None:
    op.execute(PURGE_SQL)
    op.execute(SESSIONS_SQL)
    op.execute(CREWS_SQL)
    op.execute(SCHEDULE_SQL)
    op.execute(INDEXES_SQL)


def downgrade() -> None:
    raise RuntimeError("Retention purges are forward-only")
