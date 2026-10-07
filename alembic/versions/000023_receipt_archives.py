"""Receipt archives: every receipt of a trip in one zip, built by the worker.

Revision ID: 000023_receipt_archives
Revises: 000022_weekly_summary_news
Create Date: 2026-10-08

Forward action (schema ``media_memories``):

* ``receipt_archives``: one request by one person for one plan's receipts. The worker
  builds the zip (``pending`` -> ``building`` -> ``ready`` or ``failed``), stores it
  under ``archives/<id>.zip`` and queues the object's deletion for when it expires (24
  hours later); only the person who asked may read the request and download the file.
  Rows go with their plan.
* ``media_memories.archive_entries(archive)``: the ready receipts of active (not voided)
  expenses the worker packs, with each expense's date, description, and amount, so the
  worker never reads finance tables itself.
* At most one archive in the making per person and plan (a partial unique index); a
  deleted request (its plan purged) queues its zip for deletion at once (a trigger).

* The object deletion queue also takes ``archives/<id>.zip`` keys.

Lock/scan risk: a new table and function; foreign keys to ``plans.plans`` and
``iam.users`` take brief SHARE ROW EXCLUSIVE locks; replacing the key check locks
``media_memories.object_deletions`` (ACCESS EXCLUSIVE) while it rescans that small queue.

Validation:
    SELECT relrowsecurity FROM pg_class
    WHERE oid = 'media_memories.receipt_archives'::regclass;                  -- t
    SELECT has_function_privilege('api_runtime',
        'media_memories.archive_entries(uuid)', 'EXECUTE');                    -- f

Compatibility: additive.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000023_receipt_archives"
down_revision = "000022_weekly_summary_news"
branch_labels = None
depends_on = None

SQL = """
-- Archives are deleted from storage through the same queue as media objects.
ALTER TABLE media_memories.object_deletions
    DROP CONSTRAINT object_deletions_object_key_check,
    ADD CONSTRAINT object_deletions_object_key_check CHECK (
        object_key ~ '^(incoming|media)/[0-9a-f-]{36}$'
        OR object_key ~ '^archives/[0-9a-f-]{36}[.]zip$'
    );

CREATE TABLE media_memories.receipt_archives (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id) ON DELETE CASCADE,
    requested_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    state text NOT NULL CHECK (state IN ('pending', 'building', 'ready', 'failed')),
    failure text CHECK (failure IN ('too_large', 'storage')),
    receipts integer CHECK (receipts >= 0),
    size_bytes bigint CHECK (size_bytes >= 0),
    created_at timestamptz NOT NULL,
    ready_at timestamptz,
    expires_at timestamptz,
    CHECK ((state = 'failed') = (failure IS NOT NULL))
);
CREATE INDEX receipt_archives_requester_idx
    ON media_memories.receipt_archives (plan_id, requested_by_user_id, created_at);
-- One archive in the making per person and plan (two requests at once share it).
CREATE UNIQUE INDEX receipt_archives_one_in_progress_idx
    ON media_memories.receipt_archives (plan_id, requested_by_user_id)
    WHERE state IN ('pending', 'building');
CREATE INDEX receipt_archives_in_progress_idx ON media_memories.receipt_archives (created_at)
    WHERE state IN ('pending', 'building');

-- A request that goes (its plan purged) takes its zip with it at once.
CREATE FUNCTION media_memories.forget_archive_file() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    INSERT INTO media_memories.object_deletions (object_key, requested_at)
    VALUES ('archives/' || OLD.id || '.zip', transaction_timestamp())
    ON CONFLICT (object_key) DO UPDATE SET requested_at = transaction_timestamp();
    RETURN OLD;
END;
$$;
REVOKE EXECUTE ON FUNCTION media_memories.forget_archive_file() FROM PUBLIC;
CREATE TRIGGER receipt_archives_forget_file AFTER DELETE ON media_memories.receipt_archives
    FOR EACH ROW EXECUTE FUNCTION media_memories.forget_archive_file();

ALTER TABLE media_memories.receipt_archives ENABLE ROW LEVEL SECURITY;
-- People ask for and read their own archives of plans they are in; the worker builds.
CREATE POLICY receipt_archives_own ON media_memories.receipt_archives
    FOR SELECT TO api_runtime
    USING (requested_by_user_id = iam.actor_id()
           AND plans.actor_is_active_participant(plan_id));
CREATE POLICY receipt_archives_request ON media_memories.receipt_archives
    FOR INSERT TO api_runtime
    WITH CHECK (requested_by_user_id = iam.actor_id() AND state = 'pending'
                AND plans.actor_is_active_participant(plan_id));
CREATE POLICY receipt_archives_worker ON media_memories.receipt_archives
    FOR ALL TO worker_runtime USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON media_memories.receipt_archives TO api_runtime;
GRANT SELECT, UPDATE ON media_memories.receipt_archives TO worker_runtime;

-- What goes in an archive: the plan's ready receipts, each with its expense.
CREATE FUNCTION media_memories.archive_entries(p_archive_id uuid)
    RETURNS TABLE (
        media_id uuid, content_type text, size_bytes bigint, expense_id uuid,
        occurred_on date, description text, amount_minor bigint, currency text
    )
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT m.id, m.content_type, m.size_bytes, e.id, r.occurred_on, r.description,
           r.amount_minor, r.currency::text
    FROM media_memories.receipt_archives AS a
    JOIN media_memories.media AS m ON m.plan_id = a.plan_id
    JOIN finance.expenses AS e ON e.id = m.expense_id AND e.plan_id = a.plan_id
    JOIN finance.expense_revisions AS r ON r.id = e.current_revision_id
    WHERE a.id = p_archive_id AND m.kind = 'receipt' AND m.state = 'ready'
      AND m.deleted_at IS NULL AND e.state = 'active'
    ORDER BY r.occurred_on, e.id, m.id;
$$;
REVOKE EXECUTE ON FUNCTION media_memories.archive_entries(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION media_memories.archive_entries(uuid) TO worker_runtime;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    raise RuntimeError("Receipt archives are forward-only")
