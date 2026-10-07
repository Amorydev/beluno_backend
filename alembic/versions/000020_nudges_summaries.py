"""Nudges, daily summaries, and notifications for forgiven debts.

Revision ID: 000020_nudges_summaries
Revises: 000019_notifications
Create Date: 2026-10-07

Forward action:

* ``engagement.known_zone(name)``: the zone PostgreSQL knows by that name, else UTC.
  The API accepts every IANA name Python knows; a server's zone data can lack some
  (aliases such as Asia/Saigon on builds using a trimmed system tzdata), and one
  unknown name must not stop every notification.
* ``engagement.queue_nudge(kind, plan, subject)``: someone in a plan nudges the
  assignee of an open task, or a person who owes them; once a day (the recipient's
  day) per task and recipient, or per pair. The function finds the recipient (following
  merges), builds the dedupe key itself, and returns NULL when nobody can receive it
  (a placeholder, someone gone, the nudger themself). The API decides who may nudge.
* ``engagement.queue_summaries(now)``: at 21:00 in each person's time zone, a daily
  summary of a trip in progress (how many things others did there today), once per
  trip and day, for people who keep summaries on.
* ``engagement.fan_out`` also covers forgiven debts (``waiver.given``, both sides),
  and the activity trigger queues them.
* ``engagement.queue_reminders`` counts "today" in the assignee's time zone.
* Index ``activity.events (plan_id, occurred_at)`` replaces ``events_plan_idx`` (plan_id).

Lock/scan risk: functions replaced; ``CREATE OR REPLACE TRIGGER`` takes a SHARE ROW
EXCLUSIVE lock on ``activity.events`` briefly; ``CREATE INDEX`` blocks writes to
``activity.events`` while it builds (one scan of the table, small at this stage).

Validation:
    SELECT has_function_privilege('api_runtime',
        'engagement.queue_nudge(text, uuid, uuid)', 'EXECUTE');            -- t
    SELECT has_function_privilege('worker_runtime',
        'engagement.queue_summaries(timestamptz)', 'EXECUTE');             -- t
    SELECT engagement.known_zone('Not/AZone');                              -- UTC
    SELECT pg_get_triggerdef(oid) LIKE '%waiver.given%' FROM pg_trigger
    WHERE tgname = 'events_queue_notifications';                            -- t

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000020_nudges_summaries"
down_revision = "000019_notifications"
branch_labels = None
depends_on = None

ZONE_SQL = """
-- The zone PostgreSQL knows by that name, or UTC (see the header).
CREATE FUNCTION engagement.known_zone(p_zone text) RETURNS text
    LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_zone IS NULL THEN
        RETURN 'UTC';
    END IF;
    PERFORM transaction_timestamp() AT TIME ZONE p_zone;
    RETURN p_zone;
EXCEPTION WHEN invalid_parameter_value THEN
    RETURN 'UTC';
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.known_zone(text) FROM PUBLIC;

CREATE INDEX events_plan_time_idx ON activity.events (plan_id, occurred_at)
    WHERE plan_id IS NOT NULL;
DROP INDEX activity.events_plan_idx;
"""

FAN_OUT_SQL = """
CREATE OR REPLACE TRIGGER events_queue_notifications AFTER INSERT ON activity.events
    FOR EACH ROW
    WHEN (NEW.scope_type = 'plan' AND NEW.type IN ('expense.added', 'expense.edited',
          'expense.voided', 'expense.refunded', 'payment.recorded', 'payment.reversed',
          'waiver.given'))
    EXECUTE FUNCTION engagement.queue_activity();

-- Queued events -> notifications for the people they involve (not whoever did it).
-- A participant merged into another is that other person.
CREATE OR REPLACE FUNCTION engagement.fan_out(p_now timestamptz) RETURNS integer
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
        -- Payments and forgiven debts: both sides.
        SELECT ev.id, side.participant_id
        FROM events AS ev
        JOIN finance.settlements AS t
          ON t.id = ev.entity_id AND (ev.type LIKE 'payment.%' OR ev.type = 'waiver.given'),
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

-- Reminders: tasks due today or tomorrow in the assignee's time zone (to them) and polls
-- closing within two hours (to voters who have not voted). Once per task due date and poll.
CREATE OR REPLACE FUNCTION engagement.queue_reminders(p_now timestamptz) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    added integer;
BEGIN
    WITH due AS (
        SELECT pp.user_id, 'task_due' AS kind, t.plan_id, 'task' AS entity_type,
               t.id AS entity_id,
               'task:' || t.id || ':' || t.due_date || ':' || pp.user_id AS dedupe_key,
               (t.due_date + 1)::timestamp AT TIME ZONE z.zone AS expires_at
        FROM coordination.tasks AS t
        JOIN plans.plans AS plan ON plan.id = t.plan_id
        JOIN plans.plan_participants AS pp
          ON pp.plan_id = t.plan_id AND pp.id = t.assignee_participant_id
        JOIN iam.users AS who ON who.id = pp.user_id,
             LATERAL (SELECT engagement.known_zone(who.timezone) AS zone) AS z
        WHERE t.deleted_at IS NULL AND t.status <> 'done'
          -- Somewhere on Earth it is between yesterday and tomorrow (UTC), so the due-date
          -- index narrows the scan before each assignee's own day is worked out.
          AND t.due_date BETWEEN (p_now AT TIME ZONE 'UTC')::date - 1
                             AND (p_now AT TIME ZONE 'UTC')::date + 2
          -- Today or tomorrow where the assignee is.
          AND t.due_date BETWEEN (p_now AT TIME ZONE z.zone)::date
                             AND (p_now AT TIME ZONE z.zone)::date + 1
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
"""

NUDGE_SQL = """
-- A nudge from the actor about a subject: an open task of the plan (to its assignee) or
-- a participant who owes the actor (to that participant). The recipient is whoever holds
-- the identity now (merges followed) and must be someone else, active, with an account;
-- otherwise NULL. TRUE when queued, FALSE when already nudged that day.
CREATE FUNCTION engagement.queue_nudge(p_kind text, p_plan_id uuid, p_subject_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    now_at timestamptz := transaction_timestamp();
    sender_name text;
    entity text;
    holder uuid;
    merged_into uuid;
    recipient uuid;
    zone text;
    reason text;
    added integer;
BEGIN
    SELECT display_name INTO sender_name FROM plans.plan_participants
    WHERE plan_id = p_plan_id AND user_id = actor AND access_state = 'active';
    IF actor IS NULL OR sender_name IS NULL THEN
        RAISE EXCEPTION 'only someone in the plan can nudge'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_kind = 'task_nudge' THEN
        entity := 'task';
        SELECT t.assignee_participant_id INTO holder FROM coordination.tasks AS t
        WHERE t.id = p_subject_id AND t.plan_id = p_plan_id AND t.deleted_at IS NULL
          AND t.status <> 'done';
    ELSIF p_kind = 'payment_nudge' THEN
        entity := 'plan_participant';
        holder := p_subject_id;
    ELSE
        RAISE EXCEPTION 'unknown nudge' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    FOR depth IN 1..10 LOOP
        SELECT merged_into_participant_id INTO merged_into FROM plans.plan_participants
        WHERE id = holder AND plan_id = p_plan_id;
        EXIT WHEN merged_into IS NULL;
        holder := merged_into;
    END LOOP;
    SELECT pp.user_id, engagement.known_zone(u.timezone) INTO recipient, zone
    FROM plans.plan_participants AS pp JOIN iam.users AS u ON u.id = pp.user_id
    WHERE pp.id = holder AND pp.plan_id = p_plan_id AND pp.access_state = 'active'
      AND u.status = 'active';
    IF recipient IS NULL OR recipient = actor THEN
        RETURN NULL;
    END IF;
    -- Once a day where the recipient is: per task and recipient, or per pair.
    reason := CASE p_kind
        WHEN 'task_nudge' THEN 'nudge:task:' || p_subject_id || ':' || recipient
        ELSE 'nudge:payment:' || p_plan_id || ':' || actor || ':' || recipient
    END;
    INSERT INTO engagement.notifications (
        id, user_id, category, kind, plan_id, entity_type, entity_id, args, timezone,
        dedupe_key, state, attempts, deliver_after, expires_at, created_at, sent_at
    )
    VALUES (
        gen_random_uuid(), recipient, 'reminders', p_kind, p_plan_id, entity, p_subject_id,
        jsonb_build_object('actor', sender_name,
                           'plan', (SELECT title FROM plans.plans WHERE id = p_plan_id)),
        zone, reason || ':' || (now_at AT TIME ZONE zone)::date, 'pending', 0, now_at,
        now_at + interval '1 day', now_at, NULL
    )
    ON CONFLICT (dedupe_key) DO NOTHING;
    GET DIAGNOSTICS added = ROW_COUNT;
    RETURN added = 1;
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.queue_nudge(text, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.queue_nudge(text, uuid, uuid) TO api_runtime;

-- 21:00 local: a summary of each trip in progress, if others did something there today.
-- What someone did as a guest later merged into their account counts as their own.
CREATE FUNCTION engagement.queue_summaries(p_now timestamptz) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    added integer;
BEGIN
    WITH people AS (
        SELECT pp.plan_id, pp.user_id, p.title, u.timezone,
               engagement.known_zone(u.timezone) AS zone
        FROM plans.plans AS p
        JOIN plans.plan_participants AS pp ON pp.plan_id = p.id
        JOIN iam.users AS u ON u.id = pp.user_id
        LEFT JOIN engagement.notification_settings AS s ON s.user_id = pp.user_id
        WHERE p.type = 'trip' AND p.state = 'active' AND p.deletion_scheduled_at IS NULL
          AND pp.access_state = 'active' AND u.status = 'active'
          AND coalesce(s.summaries, true)
    ),
    evening AS (
        SELECT people.*, here.day,
               'summary:' || people.plan_id || ':' || people.user_id || ':' || here.day
                   AS dedupe_key
        FROM people,
             LATERAL (SELECT (p_now AT TIME ZONE people.zone)::date AS day) AS here
        WHERE (p_now AT TIME ZONE people.zone)::time >= time '21:00'
    ),
    -- Counted only until today's summary is queued.
    counted AS (
        SELECT e.*, (
            SELECT count(*) FROM activity.events AS ev
            WHERE ev.plan_id = e.plan_id AND ev.scope_type = 'plan'
              AND ev.occurred_at >= e.day::timestamp AT TIME ZONE e.zone
              AND ev.occurred_at < p_now
              AND ev.actor_user_id IS DISTINCT FROM e.user_id
              AND NOT EXISTS (SELECT 1 FROM iam.users AS g
                              WHERE g.id = ev.actor_user_id
                                AND g.merged_into_user_id = e.user_id)
        ) AS happened
        FROM evening AS e
        WHERE NOT EXISTS (SELECT 1 FROM engagement.notifications AS n
                          WHERE n.dedupe_key = e.dedupe_key)
    ),
    inserted AS (
        INSERT INTO engagement.notifications (
            id, user_id, category, kind, plan_id, entity_type, entity_id, args, timezone,
            dedupe_key, state, attempts, deliver_after, expires_at, created_at, sent_at
        )
        SELECT gen_random_uuid(), c.user_id, 'summaries', 'daily_summary', c.plan_id, 'plan',
               c.plan_id, jsonb_build_object('plan', c.title, 'count', c.happened),
               c.timezone, c.dedupe_key, 'pending', 0, p_now,
               (c.day + 1)::timestamp AT TIME ZONE c.zone, p_now, NULL
        FROM counted AS c
        WHERE c.happened > 0
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO added FROM inserted;
    RETURN added;
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.queue_summaries(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.queue_summaries(timestamptz) TO worker_runtime;
"""


def upgrade() -> None:
    op.execute(ZONE_SQL)
    op.execute(FAN_OUT_SQL)
    op.execute(NUDGE_SQL)


def downgrade() -> None:
    raise RuntimeError("Nudges and summaries are forward-only")
