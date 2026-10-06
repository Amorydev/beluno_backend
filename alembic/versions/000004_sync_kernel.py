"""Sync kernel: per-scope change sequencing, operation records, and read/compaction gates.

Revision ID: 000004_sync_kernel
Revises: 000003_tenant_write_guards
Create Date: 2026-10-06

Forward action:
* ``sync_audit.change_log`` gains ``scope_seq`` (contiguous per scope, backfilled from
  ``server_seq`` order) and ``operation_id``; the old scope index is replaced by a
  unique ``(scope_type, scope_id, scope_seq)`` index plus a ``changed_at`` index.
* ``sync_audit.scope_heads`` holds each scope's last sequence, compaction floor, and
  generation; it is backfilled from existing change rows.
* ``sync_audit.operations`` stores the canonical response of every idempotent command.
* SECURITY DEFINER gates owned by the migrator: ``append_changes`` (the only way to
  write change rows; it locks scope heads in sorted order and assigns sequences),
  ``read_changes`` (one visibility check per call), ``compact_changes`` and
  ``purge_operations`` (worker maintenance with enforced minimum retention).
* Runtime roles lose direct INSERT on ``change_log``.

Lock/scan risk: the backfill rewrites every ``change_log`` row and ``SET NOT NULL``
plus the unique index build scan it under exclusive locks, so writes that record
changes block for the duration. The table is small before launch; on a large table
run this in a maintenance window.

Validation:
    SELECT count(*) FROM sync_audit.change_log WHERE scope_seq IS NULL;          -- 0
    SELECT has_table_privilege('api_runtime', 'sync_audit.change_log', 'INSERT'); -- f
    SELECT count(*) FROM sync_audit.scope_heads h
    JOIN (SELECT scope_type, scope_id, max(scope_seq) AS last_seq
          FROM sync_audit.change_log GROUP BY 1, 2) c USING (scope_type, scope_id)
    WHERE h.last_seq <> c.last_seq;                                                 -- 0

Compatibility: the previous build inserts change rows directly, which this revision
revokes, so the API and worker must be deployed together with it (acceptable before
launch; later sequencing changes must expand first). Clients see no change until
they call the new sync endpoints.

Rollback: forward-only. Disable sync entrypoints with the ``sync_*`` kill switches;
never truncate change rows, heads, or operation records as a rollback.
"""

from alembic import op

revision = "000004_sync_kernel"
down_revision = "000003_tenant_write_guards"
branch_labels = None
depends_on = None

CHANGE_LOG_SQL = """
ALTER TABLE sync_audit.change_log ADD COLUMN scope_seq bigint;
ALTER TABLE sync_audit.change_log ADD COLUMN operation_id uuid;
UPDATE sync_audit.change_log AS c
SET scope_seq = numbered.scope_seq
FROM (
    SELECT server_seq,
           row_number() OVER (PARTITION BY scope_type, scope_id ORDER BY server_seq) AS scope_seq
    FROM sync_audit.change_log
) AS numbered
WHERE c.server_seq = numbered.server_seq;
ALTER TABLE sync_audit.change_log ALTER COLUMN scope_seq SET NOT NULL;
ALTER TABLE sync_audit.change_log
    ADD CONSTRAINT change_log_scope_seq_check CHECK (scope_seq > 0);
CREATE UNIQUE INDEX change_log_scope_seq_key
    ON sync_audit.change_log (scope_type, scope_id, scope_seq);
DROP INDEX sync_audit.change_log_scope_idx;
CREATE INDEX change_log_changed_at_idx ON sync_audit.change_log (changed_at);

CREATE TABLE sync_audit.scope_heads (
    scope_type text NOT NULL CHECK (scope_type IN ('user', 'group', 'plan')),
    scope_id uuid NOT NULL,
    last_seq bigint NOT NULL CHECK (last_seq >= 0),
    floor_seq bigint NOT NULL CHECK (floor_seq >= 0),
    generation integer NOT NULL CHECK (generation > 0),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (scope_type, scope_id),
    CHECK (floor_seq <= last_seq)
);
INSERT INTO sync_audit.scope_heads (
    scope_type, scope_id, last_seq, floor_seq, generation, updated_at
)
SELECT scope_type, scope_id, max(scope_seq), 0, 1, max(changed_at)
FROM sync_audit.change_log
GROUP BY scope_type, scope_id;
"""

OPERATIONS_SQL = """
CREATE TABLE sync_audit.operations (
    id uuid PRIMARY KEY,
    actor_user_id uuid NOT NULL,
    command text NOT NULL CHECK (command ~ '^[a-z][a-z0-9_.]{0,63}$'),
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    source text NOT NULL CHECK (source IN ('http', 'push')),
    session_id uuid,
    device_id text CHECK (char_length(device_id) BETWEEN 1 AND 64),
    client_created_at timestamptz,
    response_status smallint NOT NULL CHECK (response_status BETWEEN 200 AND 299),
    response_body jsonb,
    response_etag text CHECK (char_length(response_etag) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    UNIQUE (actor_user_id, command, idempotency_key),
    CHECK (expires_at > created_at)
);
CREATE INDEX operations_actor_key_idx ON sync_audit.operations (actor_user_id, idempotency_key);
CREATE INDEX operations_expiry_idx ON sync_audit.operations (expires_at);
"""

FUNCTIONS_SQL = """
CREATE FUNCTION sync_audit.actor_can_view_scope(p_scope_type text, p_scope_id uuid)
    RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT CASE p_scope_type
        WHEN 'user' THEN p_scope_id = iam.actor_id()
        WHEN 'group' THEN groups.actor_can_view_group(p_scope_id)
        WHEN 'plan' THEN plans.actor_can_view_plan(p_scope_id)
        ELSE false
    END
$$;

-- The only writer of change rows. Heads are created and locked in one sorted order so
-- concurrent writers never wait on each other in a cycle; every row then takes the
-- next sequence of its scope while the head stays locked until commit, so a reader
-- that sees sequence N in a scope can never later see a smaller one appear.
CREATE FUNCTION sync_audit.append_changes(p_changes jsonb) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    head record;
    item record;
    next_seq bigint;
    appended integer := 0;
BEGIN
    IF jsonb_typeof(p_changes) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'changes must be a JSON array' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    FOR head IN
        SELECT e.value->>'scope_type' AS scope_type, (e.value->>'scope_id')::uuid AS scope_id,
               min((e.value->>'changed_at')::timestamptz) AS first_changed_at
        FROM jsonb_array_elements(p_changes) AS e(value)
        GROUP BY 1, 2
        ORDER BY 1, 2
    LOOP
        INSERT INTO sync_audit.scope_heads (
            scope_type, scope_id, last_seq, floor_seq, generation, updated_at
        ) VALUES (head.scope_type, head.scope_id, 0, 0, 1, head.first_changed_at)
        ON CONFLICT DO NOTHING;
        PERFORM 1 FROM sync_audit.scope_heads
        WHERE scope_type = head.scope_type AND scope_id = head.scope_id
        FOR UPDATE;
    END LOOP;
    FOR item IN
        SELECT e.value AS v
        FROM jsonb_array_elements(p_changes) WITH ORDINALITY AS e(value, ordinality)
        ORDER BY e.ordinality
    LOOP
        UPDATE sync_audit.scope_heads
        SET last_seq = last_seq + 1,
            updated_at = greatest(updated_at, (item.v->>'changed_at')::timestamptz)
        WHERE scope_type = item.v->>'scope_type' AND scope_id = (item.v->>'scope_id')::uuid
        RETURNING last_seq INTO next_seq;
        INSERT INTO sync_audit.change_log (
            changed_at, scope_type, scope_id, scope_seq, entity_type, entity_id,
            entity_version, operation, actor_user_id, request_id, operation_id
        ) VALUES (
            (item.v->>'changed_at')::timestamptz,
            item.v->>'scope_type',
            (item.v->>'scope_id')::uuid,
            next_seq,
            item.v->>'entity_type',
            (item.v->>'entity_id')::uuid,
            (item.v->>'entity_version')::integer,
            item.v->>'operation',
            (item.v->>'actor_user_id')::uuid,
            item.v->>'request_id',
            (item.v->>'operation_id')::uuid
        );
        appended := appended + 1;
    END LOOP;
    RETURN appended;
END;
$$;

-- Pull reads one scope through this gate: visibility is checked once per call
-- instead of once per row, and runtime roles never read change rows directly.
CREATE FUNCTION sync_audit.read_changes(
    p_scope_type text, p_scope_id uuid, p_after bigint, p_upto bigint, p_limit integer
) RETURNS TABLE (
    scope_seq bigint, entity_type text, entity_id uuid, entity_version integer,
    operation text, changed_at timestamptz
)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT sync_audit.actor_can_view_scope(p_scope_type, p_scope_id) THEN
        RETURN;
    END IF;
    RETURN QUERY
        SELECT c.scope_seq, c.entity_type, c.entity_id, c.entity_version, c.operation, c.changed_at
        FROM sync_audit.change_log AS c
        WHERE c.scope_type = p_scope_type AND c.scope_id = p_scope_id
          AND c.scope_seq > p_after AND c.scope_seq <= p_upto
        ORDER BY c.scope_seq
        LIMIT least(greatest(p_limit, 0), 1000);
END;
$$;

-- Retention: deletes change rows older than the cutoff and raises each scope's floor
-- to the highest removed sequence, so older cursors must resync. The cutoff can never
-- reach inside the supported offline window.
CREATE FUNCTION sync_audit.compact_changes(p_cutoff timestamptz, p_batch integer)
    RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    floors jsonb;
    removed integer;
    scope record;
BEGIN
    IF p_cutoff > now() - interval '90 days' THEN
        RAISE EXCEPTION 'change retention must cover the 90-day offline window'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_batch NOT BETWEEN 1 AND 100000 THEN
        RAISE EXCEPTION 'batch must be between 1 and 100000' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    WITH doomed AS (
        DELETE FROM sync_audit.change_log
        WHERE server_seq IN (
            SELECT server_seq FROM sync_audit.change_log
            WHERE changed_at < p_cutoff
            ORDER BY server_seq
            LIMIT p_batch
        )
        RETURNING scope_type, scope_id, scope_seq
    ), per_scope AS (
        SELECT scope_type, scope_id, max(scope_seq) AS floor_seq, count(*) AS removed
        FROM doomed
        GROUP BY scope_type, scope_id
    )
    SELECT coalesce(jsonb_agg(to_jsonb(per_scope) ORDER BY scope_type, scope_id), '[]'::jsonb),
           coalesce(sum(per_scope.removed), 0)
    INTO floors, removed
    FROM per_scope;
    FOR scope IN
        SELECT * FROM jsonb_to_recordset(floors)
            AS f(scope_type text, scope_id uuid, floor_seq bigint)
        ORDER BY 1, 2
    LOOP
        UPDATE sync_audit.scope_heads
        SET floor_seq = greatest(floor_seq, scope.floor_seq)
        WHERE scope_type = scope.scope_type AND scope_id = scope.scope_id;
    END LOOP;
    RETURN removed;
END;
$$;

CREATE FUNCTION sync_audit.purge_operations(p_now timestamptz, p_batch integer)
    RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    removed integer;
BEGIN
    IF p_batch NOT BETWEEN 1 AND 100000 THEN
        RAISE EXCEPTION 'batch must be between 1 and 100000' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    DELETE FROM sync_audit.operations
    WHERE id IN (
        SELECT id FROM sync_audit.operations
        WHERE expires_at < least(p_now, now())
        ORDER BY expires_at
        LIMIT p_batch
    );
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;
"""

RLS_SQL = """
ALTER TABLE sync_audit.scope_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_audit.operations ENABLE ROW LEVEL SECURITY;

-- Change rows are written and read only through the gates above.
DROP POLICY change_log_insert ON sync_audit.change_log;

CREATE POLICY scope_heads_select ON sync_audit.scope_heads FOR SELECT TO api_runtime
    USING (sync_audit.actor_can_view_scope(scope_type, scope_id));

CREATE POLICY operations_select ON sync_audit.operations FOR SELECT TO api_runtime
    USING (actor_user_id = iam.actor_id());
CREATE POLICY operations_insert ON sync_audit.operations FOR INSERT TO api_runtime
    WITH CHECK (actor_user_id = iam.actor_id());
"""

GRANTS_SQL = """
REVOKE INSERT ON sync_audit.change_log FROM api_runtime, worker_runtime;
REVOKE USAGE ON SEQUENCE sync_audit.change_log_server_seq_seq FROM api_runtime, worker_runtime;
GRANT SELECT ON sync_audit.scope_heads TO api_runtime;
GRANT SELECT, INSERT ON sync_audit.operations TO api_runtime;

REVOKE EXECUTE ON FUNCTION
    sync_audit.actor_can_view_scope(text, uuid),
    sync_audit.append_changes(jsonb),
    sync_audit.read_changes(text, uuid, bigint, bigint, integer),
    sync_audit.compact_changes(timestamptz, integer),
    sync_audit.purge_operations(timestamptz, integer)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sync_audit.actor_can_view_scope(text, uuid) TO api_runtime;
GRANT EXECUTE ON FUNCTION sync_audit.append_changes(jsonb) TO api_runtime, worker_runtime;
GRANT EXECUTE ON FUNCTION sync_audit.read_changes(text, uuid, bigint, bigint, integer)
    TO api_runtime;
GRANT EXECUTE ON FUNCTION
    sync_audit.compact_changes(timestamptz, integer),
    sync_audit.purge_operations(timestamptz, integer)
    TO worker_runtime;
"""


def upgrade() -> None:
    op.execute(CHANGE_LOG_SQL)
    op.execute(OPERATIONS_SQL)
    op.execute(FUNCTIONS_SQL)
    op.execute(RLS_SQL)
    op.execute(GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("The sync kernel is forward-only")
