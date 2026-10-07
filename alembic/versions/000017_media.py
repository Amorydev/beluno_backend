"""Media: receipts, trip covers, and the upload pipeline behind them.

Revision ID: 000017_media
Revises: 000016_passkeys
Create Date: 2026-10-07

Forward action:

* ``media_memories.media`` (in the schema created empty by the platform migration):
  one row per uploaded file of a plan: kind (``receipt`` linked to an expense,
  ``cover``, ``memory``), state (``awaiting_upload`` -> ``scanning`` -> ``ready`` or
  ``rejected`` with a reason), the declared type and size, and once clean its real
  type, size, and pixel size. Bytes live in object storage under keys derived from
  the ID; rows hold no file names or paths.
* ``media_memories.object_deletions``: object keys the worker deletes from storage
  once due (``requested_at``): queued by a trigger when a file is deleted, by the plan
  purge, and by the worker for uploads it no longer needs. The API cannot touch it.
* ``plans.plans`` gains ``cover_media_id`` (a ready cover of the plan) and
  ``album_url`` (an https link to a shared album).
* Guards: a file is added in its uploader's name and starts awaiting upload; the API
  only moves it to scanning (its uploader) or deletes it (its uploader or an
  organiser); only the worker settles it as ready or rejected.
* ``plans.purge_deleted_plan`` also queues the plan's objects for deletion and
  removes its media (before its expenses).

Lock/scan risk: new tables; two nullable columns and a foreign key on ``plans.plans``
(brief ACCESS EXCLUSIVE lock, no rewrite, the key validates against empty media);
the purge function is replaced.

Validation:
    SELECT relrowsecurity FROM pg_class WHERE oid = 'media_memories.media'::regclass; -- t
    SELECT has_table_privilege('api_runtime', 'media_memories.object_deletions',
                               'INSERT');                                          -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment; objects already uploaded stay in storage under their media IDs.
"""

from alembic import op

revision = "000017_media"
down_revision = "000016_passkeys"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE media_memories.media (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    kind text NOT NULL CHECK (kind IN ('receipt', 'cover', 'memory')),
    state text NOT NULL CHECK (state IN ('awaiting_upload', 'scanning', 'ready', 'rejected')),
    rejection text CHECK (rejection IN ('type', 'size', 'malware', 'unreadable', 'missing')),
    declared_type text NOT NULL CHECK (
        declared_type IN ('image/jpeg', 'image/png', 'image/webp', 'image/heic',
                          'application/pdf')
    ),
    declared_size bigint NOT NULL CHECK (declared_size > 0),
    content_type text CHECK (
        content_type IN ('image/jpeg', 'image/png', 'image/webp', 'application/pdf')
    ),
    size_bytes bigint CHECK (size_bytes > 0),
    width integer CHECK (width > 0),
    height integer CHECK (height > 0),
    expense_id uuid,
    uploaded_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, expense_id) REFERENCES finance.expenses (plan_id, id),
    CHECK ((kind = 'receipt') = (expense_id IS NOT NULL)),
    CHECK ((state = 'rejected') = (rejection IS NOT NULL)),
    CHECK ((state = 'ready') = (content_type IS NOT NULL AND size_bytes IS NOT NULL)),
    CHECK (content_type <> 'application/pdf' OR kind = 'receipt'),
    CHECK (declared_type <> 'application/pdf' OR kind = 'receipt')
);
CREATE INDEX media_plan_idx ON media_memories.media (plan_id, id);
CREATE INDEX media_expense_idx ON media_memories.media (expense_id) WHERE expense_id IS NOT NULL;

CREATE TABLE media_memories.object_deletions (
    object_key text PRIMARY KEY CHECK (object_key ~ '^(incoming|media)/[0-9a-f-]{36}$'),
    requested_at timestamptz NOT NULL
);

ALTER TABLE plans.plans
    ADD COLUMN cover_media_id uuid,
    ADD COLUMN album_url text
        CHECK (album_url ~ '^https://' AND char_length(album_url) <= 2048);
ALTER TABLE plans.plans
    ADD CONSTRAINT plans_cover_media_fk FOREIGN KEY (id, cover_media_id)
        REFERENCES media_memories.media (plan_id, id);
"""

GUARDS_SQL = """
-- Who may delete a file: its uploader, or an active owner/admin of its plan.
CREATE FUNCTION media_memories.actor_manages_media(p_plan_id uuid, p_uploader uuid)
    RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND user_id = iam.actor_id() AND access_state = 'active'
          AND (role IN ('owner', 'admin') OR p_uploader = iam.actor_id())
    )
$$;
REVOKE EXECUTE ON FUNCTION media_memories.actor_manages_media(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION media_memories.actor_manages_media(uuid, uuid) TO api_runtime;

CREATE FUNCTION media_memories.guard_media() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NEW.state <> 'awaiting_upload' OR NEW.deleted_at IS NOT NULL
           OR NEW.content_type IS NOT NULL
           OR NEW.uploaded_by_user_id IS DISTINCT FROM iam.actor_id() THEN
            RAISE EXCEPTION 'a file is added in its uploader''s name, awaiting upload'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.plan_id, NEW.kind, NEW.declared_type, NEW.declared_size, NEW.expense_id,
        NEW.uploaded_by_user_id, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.plan_id, OLD.kind, OLD.declared_type, OLD.declared_size,
                         OLD.expense_id, OLD.uploaded_by_user_id, OLD.created_at)
       OR OLD.deleted_at IS NOT NULL OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'media_memories.media does not allow this change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF current_user = 'worker_runtime' THEN
        -- The worker settles a scanned file, and nothing else.
        IF OLD.state <> 'scanning' OR NEW.state NOT IN ('ready', 'rejected')
           OR NEW.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'the worker only settles scanned files'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        IF (NEW.state, NEW.content_type, NEW.size_bytes, NEW.rejection)
           IS DISTINCT FROM (OLD.state, OLD.content_type, OLD.size_bytes, OLD.rejection)
           OR NOT media_memories.actor_manages_media(OLD.plan_id, OLD.uploaded_by_user_id) THEN
            RAISE EXCEPTION 'only the uploader or an organiser deletes a file'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    -- The uploader reports the upload done; the API never settles a file.
    IF OLD.state <> 'awaiting_upload' OR NEW.state <> 'scanning'
       OR NEW.uploaded_by_user_id IS DISTINCT FROM iam.actor_id()
       OR (NEW.content_type, NEW.size_bytes, NEW.width, NEW.height, NEW.rejection)
          IS DISTINCT FROM (OLD.content_type, OLD.size_bytes, OLD.width, OLD.height,
                            OLD.rejection) THEN
        RAISE EXCEPTION 'only the uploader moves a file to scanning'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER media_guard BEFORE INSERT OR UPDATE ON media_memories.media
    FOR EACH ROW EXECUTE FUNCTION media_memories.guard_media();
REVOKE EXECUTE ON FUNCTION media_memories.guard_media() FROM PUBLIC;

-- A deleted file's objects are queued here, by the database itself: no runtime role
-- can queue another file's objects.
CREATE FUNCTION media_memories.queue_deleted_objects() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    INSERT INTO media_memories.object_deletions (object_key, requested_at)
    VALUES ('incoming/' || NEW.id, NEW.deleted_at), ('media/' || NEW.id, NEW.deleted_at)
    ON CONFLICT (object_key) DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER media_deleted_objects AFTER UPDATE OF deleted_at ON media_memories.media
    FOR EACH ROW WHEN (OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
    EXECUTE FUNCTION media_memories.queue_deleted_objects();
REVOKE EXECUTE ON FUNCTION media_memories.queue_deleted_objects() FROM PUBLIC;
"""

RLS_SQL = """
ALTER TABLE media_memories.media ENABLE ROW LEVEL SECURITY;
CREATE POLICY media_select ON media_memories.media FOR SELECT TO api_runtime
    USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY media_insert ON media_memories.media FOR INSERT TO api_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY media_update ON media_memories.media FOR UPDATE TO api_runtime
    USING (plans.actor_is_active_participant(plan_id))
    WITH CHECK (plans.actor_is_active_participant(plan_id));
-- The worker processes files outside any person's session.
CREATE POLICY media_worker_select ON media_memories.media FOR SELECT TO worker_runtime
    USING (true);
CREATE POLICY media_worker_update ON media_memories.media FOR UPDATE TO worker_runtime
    USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON media_memories.media TO api_runtime;
GRANT SELECT, UPDATE ON media_memories.media TO worker_runtime;

-- Filled by the deletion trigger and the plan purge (both definer functions) and by
-- the worker for objects it leaves behind; the API has no access at all.
ALTER TABLE media_memories.object_deletions ENABLE ROW LEVEL SECURITY;
CREATE POLICY object_deletions_worker ON media_memories.object_deletions
    FOR ALL TO worker_runtime USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, DELETE ON media_memories.object_deletions TO worker_runtime;
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

    -- Media: the plan forgets its cover, every stored object is queued for the
    -- worker to delete from storage, then the rows go (before the expenses they link).
    UPDATE plans.plans SET cover_media_id = NULL WHERE id = target;
    INSERT INTO media_memories.object_deletions (object_key, requested_at)
    SELECT key, now_at
    FROM media_memories.media AS m,
         LATERAL (VALUES ('incoming/' || m.id), ('media/' || m.id)) AS keys (key)
    WHERE m.plan_id = target
    ON CONFLICT (object_key) DO NOTHING;
    DELETE FROM media_memories.media WHERE plan_id = target;

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
    op.execute(PURGE_SQL)


def downgrade() -> None:
    raise RuntimeError("Media is forward-only")
