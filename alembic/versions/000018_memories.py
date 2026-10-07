"""Memories: trip photos with captions, day, time, and place, and recap highlights.

Revision ID: 000018_memories
Revises: 000017_media
Create Date: 2026-10-07

Forward action:

* ``media_memories.media`` gains, for memories only: ``caption``, ``day`` and
  ``taken_time`` (local, sent by the app since the server strips EXIF), ``place_id``
  (a saved place of the plan), and ``in_recap`` (an organiser's highlight).
* The guard also lets the uploader or an organiser edit a memory's details, only an
  organiser mark highlights, and (for receipts) the expense's creator or a holder
  of ``expenses.manage`` delete one. An uploader or creator includes a guest account
  merged into the actor (``actor_manages_media`` is replaced to say so).
* ``media_memories.forget_memories()``: account deletion removes the person's
  memories from every plan (their objects are queued by the deletion trigger) and
  tells each plan's devices; receipts stay with the money they prove.

Lock/scan risk: five nullable or defaulted columns on ``media_memories.media`` (no
rewrite: the boolean default is dropped right after); the new checks and the foreign
key to ``schedule_places.places`` scan ``media`` under ACCESS EXCLUSIVE and hold a
SHARE ROW EXCLUSIVE lock on ``places`` until the migration commits (both are small:
media launched one release earlier). The guard and two helpers are replaced.

Validation:
    SELECT count(*) FROM media_memories.media
    WHERE kind <> 'memory' AND (caption IS NOT NULL OR in_recap);              -- 0

Compatibility: ships together with the API build that sets ``in_recap`` (NOT NULL,
no default): the previous build's inserts would fail, so deploy them at once.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000018_memories"
down_revision = "000017_media"
branch_labels = None
depends_on = None

COLUMNS_SQL = """
ALTER TABLE media_memories.media
    ADD COLUMN caption text CHECK (char_length(btrim(caption)) BETWEEN 1 AND 280),
    ADD COLUMN day date,
    ADD COLUMN taken_time time,
    ADD COLUMN place_id uuid,
    ADD COLUMN in_recap boolean NOT NULL DEFAULT false;
ALTER TABLE media_memories.media ALTER COLUMN in_recap DROP DEFAULT;
ALTER TABLE media_memories.media
    ADD CONSTRAINT media_place_fk FOREIGN KEY (plan_id, place_id)
        REFERENCES schedule_places.places (plan_id, id),
    ADD CONSTRAINT media_memory_details_check CHECK (
        kind = 'memory'
        OR (caption IS NULL AND day IS NULL AND taken_time IS NULL AND place_id IS NULL
            AND NOT in_recap)
    ),
    ADD CONSTRAINT media_taken_time_check CHECK (taken_time IS NULL OR day IS NOT NULL),
    ADD CONSTRAINT media_recap_ready_check CHECK (NOT in_recap OR state = 'ready');
CREATE INDEX media_recap_idx ON media_memories.media (plan_id) WHERE in_recap;
-- Account deletion finds a person's memories without scanning every file.
CREATE INDEX media_memory_uploader_idx ON media_memories.media (uploaded_by_user_id)
    WHERE kind = 'memory' AND deleted_at IS NULL;
"""

GUARD_SQL = """
-- The uploader includes a guest account merged into the actor (its records stay theirs).
CREATE OR REPLACE FUNCTION media_memories.actor_manages_media(p_plan_id uuid, p_uploader uuid)
    RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND user_id = iam.actor_id() AND access_state = 'active'
          AND (role IN ('owner', 'admin') OR p_uploader = iam.actor_id()
               OR (p_uploader IS NOT NULL AND iam.user_merged_into_actor(p_uploader)))
    )
$$;

-- Who may delete a file: its uploader, an active owner/admin, and for a receipt the
-- expense's creator or an active holder of expenses.manage.
CREATE FUNCTION media_memories.actor_may_delete_media(
    p_plan_id uuid, p_uploader uuid, p_kind text, p_expense_id uuid
) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT media_memories.actor_manages_media(p_plan_id, p_uploader)
        OR (p_kind = 'receipt' AND EXISTS (
            SELECT 1 FROM plans.plan_participants AS p
            WHERE p.plan_id = p_plan_id AND p.user_id = iam.actor_id()
              AND p.access_state = 'active'
              AND ('expenses.manage' = ANY (p.capabilities)
                   OR EXISTS (SELECT 1 FROM finance.expenses AS e
                              WHERE e.plan_id = p_plan_id AND e.id = p_expense_id
                                AND (e.created_by_user_id = iam.actor_id()
                                     OR iam.user_merged_into_actor(e.created_by_user_id))))
        ))
$$;
REVOKE EXECUTE ON FUNCTION media_memories.actor_may_delete_media(uuid, uuid, text, uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION media_memories.actor_may_delete_media(uuid, uuid, text, uuid)
    TO api_runtime;

CREATE OR REPLACE FUNCTION media_memories.guard_media() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NEW.state <> 'awaiting_upload' OR NEW.deleted_at IS NOT NULL
           OR NEW.content_type IS NOT NULL OR NEW.in_recap
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
           OR NEW.deleted_at IS NOT NULL
           OR (NEW.caption, NEW.day, NEW.taken_time, NEW.place_id, NEW.in_recap)
              IS DISTINCT FROM (OLD.caption, OLD.day, OLD.taken_time, OLD.place_id,
                                OLD.in_recap) THEN
            RAISE EXCEPTION 'the worker only settles scanned files'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    -- From here on the file's stored result never changes through the API.
    IF (NEW.content_type, NEW.size_bytes, NEW.width, NEW.height, NEW.rejection)
       IS DISTINCT FROM (OLD.content_type, OLD.size_bytes, OLD.width, OLD.height,
                         OLD.rejection) THEN
        RAISE EXCEPTION 'only the worker settles a file'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        IF (NEW.state, NEW.caption, NEW.day, NEW.taken_time, NEW.place_id, NEW.in_recap)
           IS DISTINCT FROM (OLD.state, OLD.caption, OLD.day, OLD.taken_time, OLD.place_id,
                             OLD.in_recap)
           OR NOT media_memories.actor_may_delete_media(
               OLD.plan_id, OLD.uploaded_by_user_id, OLD.kind, OLD.expense_id) THEN
            RAISE EXCEPTION 'this person may not delete this file'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        -- The uploader reports the upload done; nothing else moves the state here.
        IF OLD.state <> 'awaiting_upload' OR NEW.state <> 'scanning'
           OR NEW.uploaded_by_user_id IS DISTINCT FROM iam.actor_id()
           OR (NEW.caption, NEW.day, NEW.taken_time, NEW.place_id, NEW.in_recap)
              IS DISTINCT FROM (OLD.caption, OLD.day, OLD.taken_time, OLD.place_id,
                                OLD.in_recap) THEN
            RAISE EXCEPTION 'only the uploader moves a file to scanning'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    -- Highlights are an organiser's pick.
    IF NEW.in_recap IS DISTINCT FROM OLD.in_recap
       AND NOT media_memories.actor_manages_media(OLD.plan_id, NULL) THEN
        RAISE EXCEPTION 'only an organiser picks recap highlights'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- A memory's details: its uploader or an organiser.
    IF (NEW.caption, NEW.day, NEW.taken_time, NEW.place_id)
       IS DISTINCT FROM (OLD.caption, OLD.day, OLD.taken_time, OLD.place_id)
       AND NOT media_memories.actor_manages_media(OLD.plan_id, OLD.uploaded_by_user_id) THEN
        RAISE EXCEPTION 'only the uploader or an organiser edits a memory'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE EXECUTE ON FUNCTION media_memories.guard_media() FROM PUBLIC;
"""

FORGET_SQL = """
-- Account deletion: the person's memories leave every plan (receipts stay with the
-- money they prove). The deletion trigger queues their objects; each plan's devices
-- hear of it.
CREATE FUNCTION media_memories.forget_memories() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    now_at timestamptz := transaction_timestamp();
    removed integer;
    changes jsonb;
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'an actor is required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    WITH gone AS (
        UPDATE media_memories.media
        SET deleted_at = now_at, in_recap = false, version = version + 1, updated_at = now_at
        WHERE kind = 'memory' AND deleted_at IS NULL
          AND (uploaded_by_user_id = actor
               OR uploaded_by_user_id IN (SELECT id FROM iam.users
                                          WHERE merged_into_user_id = actor))
        RETURNING id, plan_id, version
    )
    SELECT count(*), coalesce(jsonb_agg(jsonb_build_object(
               'changed_at', now_at,
               'scope_type', 'plan',
               'scope_id', plan_id,
               'entity_type', 'media',
               'entity_id', id,
               'entity_version', version,
               'operation', 'delete'
           ) ORDER BY plan_id, id), '[]'::jsonb)
    INTO removed, changes
    FROM gone;
    PERFORM sync_audit.append_changes(changes);
    RETURN removed;
END;
$$;
REVOKE EXECUTE ON FUNCTION media_memories.forget_memories() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION media_memories.forget_memories() TO api_runtime;
"""


def upgrade() -> None:
    op.execute(COLUMNS_SQL)
    op.execute(GUARD_SQL)
    op.execute(FORGET_SQL)


def downgrade() -> None:
    raise RuntimeError("Memories are forward-only")
