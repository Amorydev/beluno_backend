"""The weekly planning summary and news from the team.

Revision ID: 000022_weekly_summary_news
Revises: 000021_entitlements
Create Date: 2026-10-07

Forward action:

* ``engagement.queue_weekly(now)``: on Sundays from 19:00 in each person's time zone,
  a summary of each trip still being organised (draft or planning) they are in: open
  tasks, open polls, and days until it starts; once per trip and Sunday, for people who
  keep summaries on, and only when there is something to say.
* ``engagement.queue_news(key, title_vi, body_vi, title_en, body_en, now)``: operators
  send news (``scripts/news.py``) to people who turned news on and have a signed-in
  device, in Vietnamese or English by their profile's locale; once per key and person,
  so a rerun sends nothing twice.

Lock/scan risk: new functions only.

Validation:
    SELECT has_function_privilege('worker_runtime',
        'engagement.queue_weekly(timestamptz)', 'EXECUTE');                     -- t
    SELECT has_function_privilege('api_runtime',
        'engagement.queue_news(text, text, text, text, text, timestamptz)', 'EXECUTE'); -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000022_weekly_summary_news"
down_revision = "000021_entitlements"
branch_labels = None
depends_on = None

FUNCTIONS_SQL = """
-- Sundays from 19:00 local: where each trip being organised stands. Local Sunday
-- evenings fall on UTC Sunday or Monday everywhere (UTC-12 to UTC+14); other days the
-- function returns at once, and counts are taken once per trip.
CREATE FUNCTION engagement.queue_weekly(p_now timestamptz) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    added integer;
BEGIN
    IF extract(isodow FROM p_now AT TIME ZONE 'UTC') NOT IN (7, 1) THEN
        RETURN 0;
    END IF;
    WITH people AS (
        SELECT pp.plan_id, pp.user_id, p.title, u.timezone, p.start_date, p.starts_at,
               p.timezone AS plan_zone, engagement.known_zone(u.timezone) AS zone
        FROM plans.plans AS p
        JOIN plans.plan_participants AS pp ON pp.plan_id = p.id
        JOIN iam.users AS u ON u.id = pp.user_id
        LEFT JOIN engagement.notification_settings AS s ON s.user_id = pp.user_id
        WHERE p.type = 'trip' AND p.state IN ('draft', 'planning')
          AND p.deletion_scheduled_at IS NULL
          AND pp.access_state = 'active' AND u.status = 'active'
          AND coalesce(s.summaries, true)
    ),
    sunday AS (
        SELECT people.*, here.day,
               'weekly:' || people.plan_id || ':' || people.user_id || ':' || here.day
                   AS dedupe_key
        FROM people,
             LATERAL (SELECT p_now AT TIME ZONE people.zone AS moment) AS t,
             LATERAL (SELECT t.moment::date AS day) AS here
        WHERE extract(isodow FROM t.moment) = 7 AND t.moment::time >= time '19:00'
          AND NOT EXISTS (SELECT 1 FROM engagement.notifications AS n
                          WHERE n.dedupe_key = 'weekly:' || people.plan_id || ':'
                                               || people.user_id || ':' || here.day)
    ),
    trips AS (
        SELECT DISTINCT ON (s.plan_id) s.plan_id,
               (SELECT count(*) FROM coordination.tasks AS task
                WHERE task.plan_id = s.plan_id AND task.deleted_at IS NULL
                  AND task.status <> 'done') AS open_tasks,
               (SELECT count(*) FROM decisions.polls AS poll
                WHERE poll.plan_id = s.plan_id AND poll.deleted_at IS NULL
                  AND poll.status = 'open') AS open_polls
        FROM sunday AS s
    ),
    counted AS (
        SELECT s.*, t.open_tasks, t.open_polls,
               coalesce(
                   s.start_date,
                   (s.starts_at AT TIME ZONE
                        engagement.known_zone(coalesce(s.plan_zone, s.timezone)))::date
               ) - s.day AS days_to_go
        FROM sunday AS s JOIN trips AS t ON t.plan_id = s.plan_id
    ),
    inserted AS (
        INSERT INTO engagement.notifications (
            id, user_id, category, kind, plan_id, entity_type, entity_id, args, timezone,
            dedupe_key, state, attempts, deliver_after, expires_at, created_at, sent_at
        )
        SELECT gen_random_uuid(), c.user_id, 'summaries', 'weekly_summary', c.plan_id,
               'plan', c.plan_id,
               jsonb_build_object(
                   'plan', c.title, 'tasks', c.open_tasks, 'polls', c.open_polls,
                   'days', CASE WHEN c.days_to_go >= 0 THEN c.days_to_go END
               ),
               c.timezone, c.dedupe_key, 'pending', 0, p_now,
               (c.day + 1)::timestamp AT TIME ZONE c.zone, p_now, NULL
        FROM counted AS c
        WHERE c.open_tasks > 0 OR c.open_polls > 0 OR c.days_to_go >= 0
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO added FROM inserted;
    RETURN added;
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.queue_weekly(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.queue_weekly(timestamptz) TO worker_runtime;

-- News from the team to everyone who turned news on and has a signed-in device, in
-- their language (Vietnamese for a "vi" locale, English otherwise). Once per key and
-- person; kept a week.
CREATE FUNCTION engagement.queue_news(
    p_key text, p_title_vi text, p_body_vi text, p_title_en text, p_body_en text,
    p_now timestamptz
) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    added integer;
BEGIN
    IF p_key !~ '^[a-z0-9][a-z0-9-]{0,39}$' THEN
        RAISE EXCEPTION 'a news key is a short slug' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF char_length(p_title_vi) NOT BETWEEN 1 AND 80 OR char_length(p_title_en) NOT BETWEEN 1 AND 80
       OR char_length(p_body_vi) NOT BETWEEN 1 AND 300
       OR char_length(p_body_en) NOT BETWEEN 1 AND 300 THEN
        RAISE EXCEPTION 'titles take 1-80 characters, bodies 1-300'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    WITH inserted AS (
        INSERT INTO engagement.notifications (
            id, user_id, category, kind, plan_id, entity_type, entity_id, args, timezone,
            dedupe_key, state, attempts, deliver_after, expires_at, created_at, sent_at
        )
        SELECT gen_random_uuid(), u.id, 'news', 'news', NULL, NULL, NULL,
               CASE WHEN lower(coalesce(u.locale, '')) LIKE 'vi%'
                    THEN jsonb_build_object('title', p_title_vi, 'body', p_body_vi)
                    ELSE jsonb_build_object('title', p_title_en, 'body', p_body_en) END,
               u.timezone, 'news:' || p_key || ':' || u.id, 'pending', 0, p_now,
               p_now + interval '7 days', p_now, NULL
        FROM iam.users AS u
        JOIN engagement.notification_settings AS s ON s.user_id = u.id
        WHERE u.status = 'active' AND s.news
          -- Only people a device can reach now: nothing is kept for later.
          AND EXISTS (SELECT 1 FROM engagement.push_tokens AS token
                      JOIN iam.sessions AS live ON live.id = token.session_id
                      WHERE token.user_id = u.id AND live.revoked_at IS NULL
                        AND live.idle_expires_at > p_now
                        AND live.absolute_expires_at > p_now)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO added FROM inserted;
    RETURN added;
END;
$$;
REVOKE EXECUTE ON FUNCTION engagement.queue_news(text, text, text, text, text, timestamptz)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engagement.queue_news(text, text, text, text, text, timestamptz)
    TO worker_runtime;
"""


def upgrade() -> None:
    op.execute(FUNCTIONS_SQL)


def downgrade() -> None:
    raise RuntimeError("The weekly summary and news are forward-only")
