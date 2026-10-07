"""Push notifications: device tokens, settings, and an outbox fed by activity.

Revision ID: 000019_notifications
Revises: 000018_memories
Create Date: 2026-10-07

Forward action (schema ``engagement``, created empty by the platform migration):

* ``push_tokens``: one FCM token per session (a token moves to the newest session
  that registers it). A revoked session loses its token (trigger on
  ``iam.sessions``).
* ``notification_settings``: a person's categories (money, reminders, summaries,
  news) and quiet hours; no row means the defaults (all on but news; quiet
  22:00-07:00).
* ``notifications``: the outbox. One row per person and reason (``dedupe_key``),
  carrying a localisation key and non-sensitive arguments (who, which plan), never
  amounts, codes, or addresses, and for reminders an expiry; the worker delivers
  it, respecting settings and quiet hours in the person's time zone, to devices
  whose session is still live (``engagement.live_tokens``).
* ``fan_out_queue``: a trigger on ``activity.events`` queues money events in the
  same transaction that writes them, so none is missed however late it commits.
  ``engagement.fan_out(now)`` turns queued events into notifications for the people
  they involve (following merged participants); ``engagement.queue_reminders(now)``
  adds tasks due within a day and polls closing within two hours you have not voted
  in, for plans still being organised. All are definer functions: the worker never
  reads money or plan tables itself.
* ``engagement.register_push_token`` binds a token to the caller's session;
  ``engagement.forget_settings`` removes a deleted account's settings.

Lock/scan risk: new tables and functions; foreign keys to ``iam.users`` and
``iam.sessions`` and triggers on ``iam.sessions`` and ``activity.events`` take brief
SHARE ROW EXCLUSIVE locks; a partial index on ``coordination.tasks`` (small).

Validation:
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'engagement' AND c.relkind = 'r' AND c.relrowsecurity;  -- 4
    SELECT has_table_privilege('api_runtime', 'engagement.notifications', 'SELECT'); -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000019_notifications"
down_revision = "000018_memories"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE engagement.push_tokens (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES iam.users (id),
    -- Purging an ended session takes its token with it.
    session_id uuid NOT NULL UNIQUE REFERENCES iam.sessions (id) ON DELETE CASCADE,
    token text NOT NULL UNIQUE CHECK (char_length(token) BETWEEN 20 AND 4096),
    platform text NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX push_tokens_user_idx ON engagement.push_tokens (user_id);

CREATE TABLE engagement.notification_settings (
    user_id uuid PRIMARY KEY REFERENCES iam.users (id),
    money boolean NOT NULL,
    reminders boolean NOT NULL,
    summaries boolean NOT NULL,
    news boolean NOT NULL,
    quiet_hours boolean NOT NULL,
    quiet_start time NOT NULL,
    quiet_end time NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    updated_at timestamptz NOT NULL
);

CREATE TABLE engagement.notifications (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES iam.users (id),
    category text NOT NULL CHECK (category IN ('money', 'reminders', 'summaries', 'news',
                                               'security')),
    kind text NOT NULL CHECK (kind ~ '^[a-z_]{1,48}$'),
    plan_id uuid,
    entity_type text,
    entity_id uuid,
    args jsonb NOT NULL,
    timezone text,
    dedupe_key text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('pending', 'sending', 'sent', 'skipped', 'failed')),
    attempts integer NOT NULL CHECK (attempts >= 0),
    deliver_after timestamptz NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL,
    sent_at timestamptz
);
CREATE INDEX notifications_due_idx ON engagement.notifications (deliver_after)
    WHERE state = 'pending';
CREATE INDEX notifications_created_idx ON engagement.notifications (created_at);

CREATE TABLE engagement.fan_out_queue (
    event_id uuid PRIMARY KEY,
    queued_at timestamptz NOT NULL
);

-- The per-minute reminder scan reads only open, dated tasks.
CREATE INDEX tasks_due_open_idx ON coordination.tasks (due_date)
    WHERE deleted_at IS NULL AND status <> 'done' AND due_date IS NOT NULL;
"""

RLS_SQL = """
ALTER TABLE engagement.push_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE engagement.notification_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE engagement.notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE engagement.fan_out_queue ENABLE ROW LEVEL SECURITY;

-- People see and change only their own token and settings; the worker reads them
-- to deliver and prunes tokens FCM no longer accepts.
CREATE POLICY push_tokens_own ON engagement.push_tokens FOR ALL TO api_runtime
    USING (user_id = iam.actor_id()) WITH CHECK (user_id = iam.actor_id());
CREATE POLICY push_tokens_worker ON engagement.push_tokens FOR ALL TO worker_runtime
    USING (true) WITH CHECK (true);
GRANT SELECT, DELETE ON engagement.push_tokens TO api_runtime;
GRANT SELECT, DELETE ON engagement.push_tokens TO worker_runtime;

CREATE POLICY notification_settings_own ON engagement.notification_settings
    FOR ALL TO api_runtime
    USING (user_id = iam.actor_id()) WITH CHECK (user_id = iam.actor_id());
CREATE POLICY notification_settings_worker ON engagement.notification_settings
    FOR SELECT TO worker_runtime USING (true);
GRANT SELECT, INSERT, UPDATE ON engagement.notification_settings TO api_runtime;
GRANT SELECT ON engagement.notification_settings TO worker_runtime;

-- The outbox is the worker's alone.
CREATE POLICY notifications_worker ON engagement.notifications FOR ALL TO worker_runtime
    USING (true) WITH CHECK (true);
GRANT SELECT, UPDATE, DELETE ON engagement.notifications TO worker_runtime;
"""

FUNCTIONS_SQL = """
-- Bind a push token to the caller's session (it leaves any older session first).
CREATE FUNCTION engagement.register_push_token(p_session_id uuid, p_token text, p_platform text)
    RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    now_at timestamptz := transaction_timestamp();
BEGIN
    -- Locking the session serialises two registrations from the same device.
    PERFORM 1 FROM iam.sessions WHERE id = p_session_id FOR UPDATE;
    IF actor IS NULL OR NOT EXISTS (
        SELECT 1 FROM iam.sessions
        WHERE id = p_session_id AND user_id = actor AND revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'a live session of the actor is required'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    DELETE FROM engagement.push_tokens WHERE token = p_token OR session_id = p_session_id;
    INSERT INTO engagement.push_tokens (id, user_id, session_id, token, platform, created_at,
                                        updated_at)
    VALUES (gen_random_uuid(), actor, p_session_id, p_token, p_platform, now_at, now_at);
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.register_push_token(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.register_push_token(uuid, text, text) TO api_runtime;

-- A revoked session (logout, remote sign-out, account deletion) loses its token.
CREATE FUNCTION engagement.drop_revoked_session_token() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    DELETE FROM engagement.push_tokens WHERE session_id = NEW.id;
    RETURN NEW;
END;
$$;
CREATE TRIGGER sessions_drop_push_token AFTER UPDATE OF revoked_at ON iam.sessions
    FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
    EXECUTE FUNCTION engagement.drop_revoked_session_token();
REVOKE EXECUTE ON FUNCTION engagement.drop_revoked_session_token() FROM PUBLIC;

-- Account deletion removes the person's settings (tokens go with their sessions).
CREATE FUNCTION engagement.forget_settings() RETURNS void
    LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DELETE FROM engagement.notification_settings WHERE user_id = iam.actor_id();
    DELETE FROM engagement.notifications WHERE user_id = iam.actor_id() AND state = 'pending';
$$;
REVOKE EXECUTE ON FUNCTION engagement.forget_settings() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.forget_settings() TO api_runtime;

-- Money events are queued in the transaction that records them.
CREATE FUNCTION engagement.queue_activity() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    INSERT INTO engagement.fan_out_queue (event_id, queued_at)
    VALUES (NEW.id, NEW.occurred_at);
    RETURN NEW;
END;
$$;
CREATE TRIGGER events_queue_notifications AFTER INSERT ON activity.events
    FOR EACH ROW
    WHEN (NEW.scope_type = 'plan' AND NEW.type IN ('expense.added', 'expense.edited',
          'expense.voided', 'expense.refunded', 'payment.recorded', 'payment.reversed'))
    EXECUTE FUNCTION engagement.queue_activity();
REVOKE EXECUTE ON FUNCTION engagement.queue_activity() FROM PUBLIC;

-- Queued events -> notifications for the people they involve (not whoever did it).
-- A participant merged into another is that other person.
CREATE FUNCTION engagement.fan_out(p_now timestamptz) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    added integer;
BEGIN
    WITH taken AS (
        DELETE FROM engagement.fan_out_queue
        WHERE event_id IN (SELECT event_id FROM engagement.fan_out_queue
                           ORDER BY queued_at, event_id LIMIT 1000
                           FOR UPDATE SKIP LOCKED)
        RETURNING event_id
    ),
    events AS (
        SELECT e.* FROM activity.events AS e JOIN taken AS t ON t.event_id = e.id
    ),
    involved AS (
        -- Expenses: everyone who paid or shares the current revision.
        SELECT ev.id AS event_id, s.participant_id
        FROM events AS ev
        JOIN finance.expenses AS x ON x.id = ev.entity_id AND ev.type LIKE 'expense.%'
        JOIN finance.expense_splits AS s ON s.revision_id = x.current_revision_id
        UNION
        SELECT ev.id, p.participant_id
        FROM events AS ev
        JOIN finance.expenses AS x ON x.id = ev.entity_id AND ev.type LIKE 'expense.%'
        JOIN finance.expense_payers AS p ON p.revision_id = x.current_revision_id
        WHERE p.participant_id IS NOT NULL
        UNION
        -- Payments: both sides.
        SELECT ev.id, side.participant_id
        FROM events AS ev
        JOIN finance.settlements AS t ON t.id = ev.entity_id AND ev.type LIKE 'payment.%',
             LATERAL (VALUES (t.from_participant_id), (t.to_participant_id))
                 AS side (participant_id)
    ),
    recipients AS (
        SELECT DISTINCT ev.id AS event_id, ev.type, ev.plan_id, ev.entity_type, ev.entity_id,
               ev.actor_user_id, pp.user_id
        FROM involved AS i
        JOIN events AS ev ON ev.id = i.event_id
        JOIN plans.plan_participants AS named
          ON named.id = i.participant_id AND named.plan_id = ev.plan_id
        JOIN plans.plan_participants AS pp
          ON pp.id = coalesce(named.merged_into_participant_id, named.id)
         AND pp.plan_id = ev.plan_id
        WHERE pp.access_state = 'active' AND pp.user_id IS NOT NULL
          AND pp.user_id IS DISTINCT FROM ev.actor_user_id
    ),
    inserted AS (
        INSERT INTO engagement.notifications (
            id, user_id, category, kind, plan_id, entity_type, entity_id, args, timezone,
            dedupe_key, state, attempts, deliver_after, expires_at, created_at, sent_at
        )
        SELECT gen_random_uuid(), r.user_id, 'money', replace(r.type, '.', '_'), r.plan_id,
               r.entity_type, r.entity_id,
               jsonb_build_object(
                   'actor', coalesce((SELECT a.display_name FROM plans.plan_participants AS a
                                      WHERE a.plan_id = r.plan_id
                                        AND a.user_id = r.actor_user_id
                                        AND a.access_state <> 'merged'
                                      LIMIT 1), ''),
                   'plan', (SELECT title FROM plans.plans WHERE id = r.plan_id)
               ),
               u.timezone, r.event_id || ':' || r.user_id, 'pending', 0, p_now, NULL, p_now,
               NULL
        FROM recipients AS r
        JOIN iam.users AS u ON u.id = r.user_id AND u.status = 'active'
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO added FROM inserted;
    RETURN added;
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.fan_out(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.fan_out(timestamptz) TO worker_runtime;

-- The devices to reach: tokens whose session is still live.
CREATE FUNCTION engagement.live_tokens(p_user_ids uuid[], p_now timestamptz)
    RETURNS TABLE (id uuid, user_id uuid, token text, platform text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT t.id, t.user_id, t.token, t.platform
    FROM engagement.push_tokens AS t
    JOIN iam.sessions AS s ON s.id = t.session_id
    WHERE t.user_id = ANY (p_user_ids) AND s.revoked_at IS NULL
      AND s.idle_expires_at > p_now AND s.absolute_expires_at > p_now
$$;
REVOKE EXECUTE ON FUNCTION engagement.live_tokens(uuid[], timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.live_tokens(uuid[], timestamptz) TO worker_runtime;

-- Reminders: tasks due within a day (to their assignee) and polls closing within
-- two hours (to voters who have not voted). Once per task due date and per poll.
CREATE FUNCTION engagement.queue_reminders(p_now timestamptz) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    added integer;
BEGIN
    WITH due AS (
        SELECT pp.user_id, 'task_due' AS kind, t.plan_id, 'task' AS entity_type,
               t.id AS entity_id,
               'task:' || t.id || ':' || t.due_date || ':' || pp.user_id AS dedupe_key,
               (t.due_date + 1)::timestamp AT TIME ZONE 'UTC' AS expires_at
        FROM coordination.tasks AS t
        JOIN plans.plans AS plan ON plan.id = t.plan_id
        JOIN plans.plan_participants AS pp
          ON pp.plan_id = t.plan_id AND pp.id = t.assignee_participant_id
        WHERE t.deleted_at IS NULL AND t.status <> 'done' AND t.due_date IS NOT NULL
          AND t.due_date BETWEEN (p_now AT TIME ZONE 'UTC')::date
                             AND (p_now AT TIME ZONE 'UTC')::date + 1
          AND pp.access_state = 'active' AND pp.user_id IS NOT NULL
          AND plan.state IN ('draft', 'planning', 'active', 'settling')
          AND plan.deletion_scheduled_at IS NULL
        UNION ALL
        SELECT pp.user_id, 'poll_closing', p.plan_id, 'poll', p.id,
               'poll:' || p.id || ':' || pp.user_id, p.deadline_at
        FROM decisions.polls AS p
        JOIN plans.plans AS plan ON plan.id = p.plan_id
        JOIN decisions.poll_electorate AS e ON e.poll_id = p.id
        JOIN plans.plan_participants AS pp
          ON pp.plan_id = p.plan_id AND pp.id = e.participant_id
        WHERE p.deleted_at IS NULL AND p.status = 'open' AND p.deadline_at IS NOT NULL
          AND p.deadline_at > p_now AND p.deadline_at <= p_now + interval '2 hours'
          AND pp.access_state = 'active' AND pp.user_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM decisions.poll_votes AS v
                          WHERE v.poll_id = p.id AND v.participant_id = e.participant_id)
          AND plan.state IN ('draft', 'planning', 'active', 'settling')
          AND plan.deletion_scheduled_at IS NULL
    ),
    inserted AS (
        INSERT INTO engagement.notifications (
            id, user_id, category, kind, plan_id, entity_type, entity_id, args, timezone,
            dedupe_key, state, attempts, deliver_after, expires_at, created_at, sent_at
        )
        SELECT gen_random_uuid(), d.user_id, 'reminders', d.kind, d.plan_id, d.entity_type,
               d.entity_id,
               jsonb_build_object('plan', (SELECT title FROM plans.plans WHERE id = d.plan_id)),
               u.timezone, d.dedupe_key, 'pending', 0, p_now, d.expires_at, p_now, NULL
        FROM due AS d
        JOIN iam.users AS u ON u.id = d.user_id AND u.status = 'active'
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO added FROM inserted;
    RETURN added;
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.queue_reminders(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.queue_reminders(timestamptz) TO worker_runtime;
"""


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(RLS_SQL)
    op.execute(FUNCTIONS_SQL)


def downgrade() -> None:
    raise RuntimeError("Notifications are forward-only")
