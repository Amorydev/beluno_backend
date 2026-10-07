"""Paid plans: purchases verified with the App Store and Google Play, and what they unlock.

Revision ID: 000021_entitlements
Revises: 000020_nudges_summaries
Create Date: 2026-10-07

Forward action (schema ``billing``, created empty by the platform migration):

* ``purchases``: one row per store purchase (``store`` plus the store's lasting id:
  Apple's original transaction id, Google's purchase token), bound to the account that
  recorded it first and, for a Trip Pass, to one trip for good. Pro carries the end of
  its latest period; refunds revoke the period they cover, a later paid period lifts
  that, and only a store notification reverses a refund. Nothing a store signs is kept
  beyond these fields.
* ``billing.record_purchase``: the API records a purchase it has verified with the
  store; replaying it merges the store's word and never moves it to another person or
  trip (the answer says so instead). ``billing.update_purchase``: the same for a
  verified store notification. Both go through ``billing.merge_store_state``.
* ``billing.acknowledged`` / ``billing.awaiting_acknowledgement``: the worker confirms
  Google purchases (Google refunds those left unconfirmed for three days).
* ``billing.plan_unlock(plan, now)``: ``trip_pass`` or ``pro`` (the owner's) when a trip
  is unlocked; ``billing.trips_counting(owner, except, now)``: a person's own trips in
  progress that count toward the free limit (NULL with Pro).

Lock/scan risk: new table and functions; the foreign keys to ``iam.users`` and
``plans.plans`` take brief SHARE ROW EXCLUSIVE locks.

Validation:
    SELECT relrowsecurity FROM pg_class WHERE oid = 'billing.purchases'::regclass; -- t
    SELECT has_table_privilege('api_runtime', 'billing.purchases', 'INSERT');     -- f
    SELECT has_function_privilege('worker_runtime',
        'billing.acknowledged(uuid, timestamptz)', 'EXECUTE');                     -- t
    SELECT has_function_privilege('api_runtime',
        'billing.merge_store_state(uuid, timestamptz, timestamptz, boolean)', 'EXECUTE'); -- f

Compatibility: additive; limits apply only once their settings are set.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000021_entitlements"
down_revision = "000020_nudges_summaries"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE billing.purchases (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES iam.users (id),
    store text NOT NULL CHECK (store IN ('apple', 'google')),
    product text NOT NULL CHECK (product IN ('trip_pass', 'pro')),
    product_id text NOT NULL CHECK (char_length(product_id) BETWEEN 1 AND 200),
    original_id text NOT NULL CHECK (char_length(original_id) BETWEEN 1 AND 4096),
    -- The trip a pass unlocks; it outlives a purged plan as a record of the sale.
    plan_id uuid REFERENCES plans.plans (id) ON DELETE SET NULL,
    environment text NOT NULL CHECK (char_length(environment) BETWEEN 1 AND 40),
    purchased_at timestamptz NOT NULL,
    expires_at timestamptz,
    revoked_at timestamptz,
    -- Google: replaced by another subscription (an upgrade or a resubscription).
    superseded_at timestamptz,
    acknowledged_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (store, original_id),
    CHECK (product = 'pro' OR expires_at IS NULL)
);
CREATE INDEX purchases_user_idx ON billing.purchases (user_id);
CREATE INDEX purchases_plan_idx ON billing.purchases (plan_id) WHERE plan_id IS NOT NULL;
CREATE INDEX purchases_unacknowledged_idx ON billing.purchases (created_at)
    WHERE store = 'google' AND acknowledged_at IS NULL;

ALTER TABLE billing.purchases ENABLE ROW LEVEL SECURITY;
-- People read their own purchases; every write goes through the functions below.
CREATE POLICY purchases_own ON billing.purchases FOR SELECT TO api_runtime
    USING (user_id = iam.actor_id());
REVOKE ALL ON SCHEMA billing FROM PUBLIC;
GRANT USAGE ON SCHEMA billing TO api_runtime, worker_runtime;
GRANT SELECT ON billing.purchases TO api_runtime;
"""

FUNCTIONS_SQL = """
-- How the store's word changes a purchase. ``expires_at`` is the end of Pro's latest
-- known period and ``revoked_at`` a refund or revocation of that period (or of a pass).
-- A newer period replaces both (a renewal after a refund was paid for); an older period
-- changes nothing; the same period can be revoked, and only a store notification
-- (``p_trusted``) lifts a revocation (a refund reversed), never a copy the app sends
-- again. Expiries are bounded: no Pro period runs past two years from now.
CREATE FUNCTION billing.merge_store_state(
    p_id uuid, p_expires_at timestamptz, p_revoked_at timestamptz, p_trusted boolean
) RETURNS void
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    known billing.purchases;
    newer boolean;
    same boolean;
BEGIN
    IF p_expires_at IS NOT NULL AND (NOT isfinite(p_expires_at)
            OR p_expires_at > transaction_timestamp() + interval '2 years') THEN
        RAISE EXCEPTION 'an expiry past any period sold' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT * INTO known FROM billing.purchases WHERE id = p_id FOR UPDATE;
    newer := p_expires_at IS NOT NULL
             AND (known.expires_at IS NULL OR p_expires_at > known.expires_at);
    same := p_expires_at IS NULL OR p_expires_at = known.expires_at;
    UPDATE billing.purchases
    SET expires_at = CASE WHEN known.product = 'pro' AND newer THEN p_expires_at
                          ELSE known.expires_at END,
        revoked_at = CASE
            WHEN newer THEN p_revoked_at
            WHEN NOT same THEN known.revoked_at
            WHEN p_revoked_at IS NOT NULL THEN coalesce(known.revoked_at, p_revoked_at)
            WHEN p_trusted THEN NULL
            ELSE known.revoked_at
        END,
        updated_at = transaction_timestamp()
    WHERE id = p_id;
END;
$$;
REVOKE EXECUTE ON FUNCTION billing.merge_store_state(uuid, timestamptz, timestamptz, boolean)
    FROM PUBLIC;

-- Records a purchase the API verified with the store, for the actor; replays merge the
-- store's word as above. ``outcome``: 'recorded', 'updated', 'owned_elsewhere' (another
-- account recorded it), or 'used_elsewhere' (a pass already unlocks another trip); the
-- stored row is returned either way. ``p_replaces``: the Google subscription this one
-- replaced (an upgrade or a resubscription), which stops counting.
CREATE FUNCTION billing.record_purchase(
    p_id uuid, p_store text, p_product text, p_product_id text, p_original_id text,
    p_plan_id uuid, p_environment text, p_purchased_at timestamptz,
    p_expires_at timestamptz, p_revoked_at timestamptz, p_replaces text
) RETURNS TABLE (
    outcome text, purchase_id uuid, plan_id uuid, expires_at timestamptz,
    revoked_at timestamptz
)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    actor uuid := iam.actor_id();
    now_at timestamptz := transaction_timestamp();
    known billing.purchases;
    result text := 'updated';
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'an actor is required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (p_product = 'trip_pass') <> (p_plan_id IS NOT NULL) THEN
        RAISE EXCEPTION 'a Trip Pass, and only a Trip Pass, names its trip'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_plan_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM plans.plans AS p
        JOIN plans.plan_participants AS pp ON pp.plan_id = p.id
        WHERE p.id = p_plan_id AND p.type = 'trip' AND pp.user_id = actor
          AND pp.access_state = 'active'
    ) THEN
        RAISE EXCEPTION 'a Trip Pass goes on a trip the buyer is in'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO billing.purchases (
        id, user_id, store, product, product_id, original_id, plan_id, environment,
        purchased_at, expires_at, revoked_at, superseded_at, acknowledged_at, created_at,
        updated_at
    ) VALUES (
        p_id, actor, p_store, p_product, p_product_id, p_original_id, p_plan_id,
        p_environment, p_purchased_at, NULL, NULL, NULL, NULL, now_at, now_at
    )
    ON CONFLICT (store, original_id) DO NOTHING;
    IF FOUND THEN
        result := 'recorded';
    END IF;
    SELECT * INTO known FROM billing.purchases AS b
    WHERE b.store = p_store AND b.original_id = p_original_id
    FOR UPDATE;
    IF known.user_id <> actor THEN
        result := 'owned_elsewhere';
    ELSIF known.product = 'trip_pass' AND known.plan_id IS DISTINCT FROM p_plan_id THEN
        result := 'used_elsewhere';
    ELSE
        PERFORM billing.merge_store_state(known.id, p_expires_at, p_revoked_at, false);
        IF p_replaces IS NOT NULL AND p_store = 'google' THEN
            UPDATE billing.purchases AS b SET superseded_at = now_at, updated_at = now_at
            WHERE b.store = 'google' AND b.original_id = p_replaces AND b.product = 'pro'
              AND b.id <> known.id AND b.superseded_at IS NULL;
        END IF;
    END IF;
    RETURN QUERY
        SELECT result, b.id, b.plan_id, b.expires_at, b.revoked_at
        FROM billing.purchases AS b WHERE b.id = known.id;
END;
$$;
REVOKE EXECUTE ON FUNCTION billing.record_purchase(
    uuid, text, text, text, text, uuid, text, timestamptz, timestamptz, timestamptz, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing.record_purchase(
    uuid, text, text, text, text, uuid, text, timestamptz, timestamptz, timestamptz, text
) TO api_runtime;

-- A verified store notification: renewals, expiries, refunds, and reversed refunds.
-- Unknown purchases are left for the app to record. Returns the purchase, if known.
CREATE FUNCTION billing.update_purchase(
    p_store text, p_original_id text, p_expires_at timestamptz, p_revoked_at timestamptz
) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    known uuid;
BEGIN
    SELECT id INTO known FROM billing.purchases
    WHERE store = p_store AND original_id = p_original_id;
    IF known IS NOT NULL THEN
        PERFORM billing.merge_store_state(known, p_expires_at, p_revoked_at, true);
    END IF;
    RETURN known;
END;
$$;
REVOKE EXECUTE ON FUNCTION billing.update_purchase(text, text, timestamptz, timestamptz)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing.update_purchase(text, text, timestamptz, timestamptz)
    TO api_runtime;

-- Google purchases still to confirm, and confirming one.
CREATE FUNCTION billing.awaiting_acknowledgement(p_id uuid)
    RETURNS TABLE (product text, product_id text, original_id text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT b.product, b.product_id, b.original_id FROM billing.purchases AS b
    WHERE b.id = p_id AND b.store = 'google' AND b.acknowledged_at IS NULL
      AND b.revoked_at IS NULL AND b.superseded_at IS NULL;
$$;
REVOKE EXECUTE ON FUNCTION billing.awaiting_acknowledgement(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing.awaiting_acknowledgement(uuid) TO worker_runtime;

CREATE FUNCTION billing.acknowledged(p_id uuid, p_now timestamptz) RETURNS void
    LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    UPDATE billing.purchases SET acknowledged_at = p_now, updated_at = p_now
    WHERE id = p_id AND acknowledged_at IS NULL;
$$;
REVOKE EXECUTE ON FUNCTION billing.acknowledged(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing.acknowledged(uuid, timestamptz) TO worker_runtime;

-- Whether a person holds Pro now.
CREATE FUNCTION billing.has_pro(p_user_id uuid, p_now timestamptz) RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM billing.purchases AS b
        WHERE b.user_id = p_user_id AND b.product = 'pro' AND b.revoked_at IS NULL
          AND b.superseded_at IS NULL AND b.expires_at > p_now
    );
$$;
REVOKE EXECUTE ON FUNCTION billing.has_pro(uuid, timestamptz) FROM PUBLIC;

-- What unlocks a trip the actor is in: its own Trip Pass, else the owner's Pro.
CREATE FUNCTION billing.plan_unlock(p_plan_id uuid, p_now timestamptz) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT CASE
        WHEN NOT plans.actor_is_active_participant(p_plan_id) THEN NULL
        WHEN EXISTS (SELECT 1 FROM billing.purchases AS b
                     WHERE b.plan_id = p_plan_id AND b.product = 'trip_pass'
                       AND b.revoked_at IS NULL) THEN 'trip_pass'
        WHEN EXISTS (SELECT 1 FROM plans.plan_participants AS owner
                     WHERE owner.plan_id = p_plan_id AND owner.role = 'owner'
                       AND owner.access_state = 'active'
                       AND billing.has_pro(owner.user_id, p_now)) THEN 'pro'
    END;
$$;
REVOKE EXECUTE ON FUNCTION billing.plan_unlock(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing.plan_unlock(uuid, timestamptz) TO api_runtime;

-- The free limit for a person's own trips: how many are in progress without a Trip
-- Pass (``p_except`` aside), or NULL when Pro lifts the limit. Asked by the person
-- themself, or about someone in a trip the actor is in (reopening, restoring, or
-- handing over that trip).
CREATE FUNCTION billing.trips_counting(p_owner uuid, p_except uuid, p_now timestamptz)
    RETURNS integer
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_owner IS DISTINCT FROM iam.actor_id() AND NOT (
        plans.actor_is_active_participant(p_except)
        AND EXISTS (SELECT 1 FROM plans.plan_participants
                    WHERE plan_id = p_except AND user_id = p_owner
                      AND access_state = 'active')
    ) THEN
        RAISE EXCEPTION 'only about oneself or someone in a shared trip'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF billing.has_pro(p_owner, p_now) THEN
        RETURN NULL;
    END IF;
    RETURN (
        SELECT count(*)::integer
        FROM plans.plans AS p
        JOIN plans.plan_participants AS owner ON owner.plan_id = p.id
        WHERE owner.user_id = p_owner AND owner.role = 'owner'
          AND owner.access_state = 'active'
          AND p.type = 'trip' AND p.state IN ('draft', 'planning', 'active', 'settling')
          AND p.deletion_scheduled_at IS NULL
          AND p.id IS DISTINCT FROM p_except
          AND NOT EXISTS (SELECT 1 FROM billing.purchases AS b
                          WHERE b.plan_id = p.id AND b.product = 'trip_pass'
                            AND b.revoked_at IS NULL)
    );
END;
$$;
REVOKE EXECUTE ON FUNCTION billing.trips_counting(uuid, uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing.trips_counting(uuid, uuid, timestamptz) TO api_runtime;
"""


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(FUNCTIONS_SQL)


def downgrade() -> None:
    raise RuntimeError("Entitlements are forward-only")
