"""Identity, groups, plans, and the audit/change recording spine.

Revision ID: 000002_identity_groups_plans
Revises: 000001_platform
Create Date: 2026-10-06

Forward action: creates new tables only (iam, groups, plans, sync_audit), their
constraints, RLS helper functions, row-level security policies, and runtime
grants. Nothing existing is rewritten.

Lock/scan risk: none on existing data; every statement targets a new object.

Validation:
    SELECT relname, relrowsecurity FROM pg_class
    WHERE relnamespace IN ('iam'::regnamespace, 'groups'::regnamespace,
                           'plans'::regnamespace, 'sync_audit'::regnamespace)
      AND relkind = 'r';
    -- every row must report relrowsecurity = true

Compatibility: additive. The previous application build ignores these tables.

Rollback: forward-only. Disable the identity/invite entrypoints with the
configuration kill switches instead of dropping tables; participant, audit, and
change rows are never deleted or rewritten as a rollback.

Runtime roles (api_runtime, worker_runtime) must exist before this revision.
Helper functions are SECURITY DEFINER and owned by the migrator so policy
checks see current relationships without recursive RLS evaluation; runtime
roles are never table owners and never hold BYPASSRLS.
"""

from alembic import op

revision = "000002_identity_groups_plans"
down_revision = "000001_platform"
branch_labels = None
depends_on = None

PLAN_KINDS = "('dinner', 'coffee', 'movie', 'sport', 'birthday', 'outing', 'trip', 'custom')"

TABLES_SQL = f"""
CREATE TABLE iam.users (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('registered', 'guest')),
    status text NOT NULL CHECK (status IN ('active', 'disabled')),
    display_name text NOT NULL CHECK (char_length(btrim(display_name)) BETWEEN 1 AND 80),
    email text CHECK (email = lower(email) AND char_length(email) BETWEEN 3 AND 320),
    email_verified_at timestamptz,
    locale text CHECK (char_length(locale) BETWEEN 2 AND 35),
    timezone text CHECK (char_length(timezone) BETWEEN 1 AND 64),
    merged_into_user_id uuid REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK ((email IS NULL) = (email_verified_at IS NULL)),
    CHECK (kind = 'registered' OR email IS NULL)
);
CREATE UNIQUE INDEX users_email_key ON iam.users (email) WHERE email IS NOT NULL;

CREATE TABLE iam.user_identities (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES iam.users (id),
    provider text NOT NULL CHECK (provider IN ('google', 'apple')),
    subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL,
    last_used_at timestamptz NOT NULL,
    UNIQUE (provider, subject)
);
CREATE INDEX user_identities_user_idx ON iam.user_identities (user_id);

CREATE TABLE iam.sessions (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES iam.users (id),
    auth_method text NOT NULL CHECK (auth_method IN ('google', 'apple', 'email', 'guest_invite')),
    authenticated_at timestamptz NOT NULL,
    client_device_id text CHECK (char_length(client_device_id) BETWEEN 1 AND 64),
    device_label text CHECK (char_length(device_label) BETWEEN 1 AND 80),
    platform text CHECK (platform IN ('ios', 'android', 'web', 'other')),
    app_version text CHECK (char_length(app_version) BETWEEN 1 AND 32),
    created_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    idle_expires_at timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_reason text CHECK (
        revoked_reason IN ('logout', 'user_revoked', 'refresh_reuse', 'account_merged')
    ),
    CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
);
CREATE INDEX sessions_user_live_idx ON iam.sessions (user_id) WHERE revoked_at IS NULL;

CREATE TABLE iam.refresh_tokens (
    token_hash bytea PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES iam.sessions (id),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    replaced_by_hash bytea
);
CREATE INDEX refresh_tokens_session_idx ON iam.refresh_tokens (session_id);

CREATE TABLE iam.email_challenges (
    id uuid PRIMARY KEY,
    email text NOT NULL CHECK (email = lower(email) AND char_length(email) BETWEEN 3 AND 320),
    code_hash bytea,
    link_token_hash bytea UNIQUE,
    delivery_state text NOT NULL CHECK (delivery_state IN ('pending', 'sending', 'sent')),
    delivered_at timestamptz,
    failed_attempts integer NOT NULL CHECK (failed_attempts >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL
);
CREATE INDEX email_challenges_expiry_idx ON iam.email_challenges (expires_at);

CREATE TABLE iam.rate_limit_counters (
    bucket bytea NOT NULL,
    window_start timestamptz NOT NULL,
    hits integer NOT NULL CHECK (hits > 0),
    PRIMARY KEY (bucket, window_start)
);
CREATE INDEX rate_limit_counters_window_idx ON iam.rate_limit_counters (window_start);

CREATE TABLE groups.groups (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
    default_currency char(3) NOT NULL CHECK (default_currency ~ '^[A-Z]{{3}}$'),
    default_timezone text NOT NULL CHECK (char_length(default_timezone) BETWEEN 1 AND 64),
    state text NOT NULL CHECK (state IN ('active', 'deletion_scheduled')),
    deletion_scheduled_at timestamptz,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK ((state = 'deletion_scheduled') = (deletion_scheduled_at IS NOT NULL))
);

CREATE TABLE groups.group_memberships (
    group_id uuid NOT NULL REFERENCES groups.groups (id),
    user_id uuid NOT NULL REFERENCES iam.users (id),
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    state text NOT NULL CHECK (state IN ('invited', 'active', 'left', 'removed')),
    invited_by_user_id uuid REFERENCES iam.users (id),
    joined_at timestamptz,
    left_at timestamptz,
    removed_at timestamptz,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (group_id, user_id),
    CHECK (role <> 'owner' OR state = 'active')
);
CREATE TABLE groups.group_invites (
    id uuid PRIMARY KEY,
    group_id uuid NOT NULL REFERENCES groups.groups (id),
    token_hash bytea NOT NULL UNIQUE,
    role text NOT NULL CHECK (role IN ('admin', 'member')),
    max_uses integer CHECK (max_uses BETWEEN 1 AND 1000),
    use_count integer NOT NULL CHECK (use_count >= 0),
    expires_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'revoked')),
    revoked_at timestamptz,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (max_uses IS NULL OR use_count <= max_uses),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);
CREATE INDEX group_invites_group_idx ON groups.group_invites (group_id);

CREATE UNIQUE INDEX group_memberships_single_owner
    ON groups.group_memberships (group_id) WHERE role = 'owner';
CREATE INDEX group_memberships_user_idx
    ON groups.group_memberships (user_id) WHERE state IN ('active', 'invited');

CREATE TABLE plans.plan_series (
    id uuid PRIMARY KEY,
    group_id uuid REFERENCES groups.groups (id),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    title text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
    kind text NOT NULL CHECK (kind IN {PLAN_KINDS}),
    base_currency char(3) NOT NULL CHECK (base_currency ~ '^[A-Z]{{3}}$'),
    visibility text NOT NULL CHECK (visibility IN ('group', 'participants')),
    description text CHECK (char_length(description) <= 2000),
    location_label text CHECK (char_length(location_label) <= 200),
    timezone text NOT NULL CHECK (char_length(timezone) BETWEEN 1 AND 64),
    start_date date NOT NULL,
    local_start_time time,
    duration_minutes integer CHECK (duration_minutes BETWEEN 1 AND 10080),
    recurrence_rule text NOT NULL CHECK (char_length(recurrence_rule) BETWEEN 1 AND 500),
    participant_user_ids uuid[] NOT NULL,
    horizon_days integer NOT NULL CHECK (horizon_days BETWEEN 1 AND 366),
    materialized_through date,
    state text NOT NULL CHECK (state IN ('active', 'cancelled')),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (visibility = 'participants' OR group_id IS NOT NULL),
    CHECK (duration_minutes IS NULL OR local_start_time IS NOT NULL)
);
CREATE INDEX plan_series_active_idx ON plans.plan_series (id) WHERE state = 'active';

CREATE TABLE plans.plans (
    id uuid PRIMARY KEY,
    group_id uuid REFERENCES groups.groups (id),
    series_id uuid REFERENCES plans.plan_series (id),
    occurrence_key text CHECK (occurrence_key ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'),
    is_series_exception boolean NOT NULL,
    title text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
    kind text NOT NULL CHECK (kind IN {PLAN_KINDS}),
    state text NOT NULL CHECK (
        state IN ('draft', 'planning', 'active', 'settling', 'completed', 'archived', 'cancelled')
    ),
    timing_mode text NOT NULL CHECK (timing_mode IN ('undecided', 'date', 'datetime')),
    start_date date,
    end_date date,
    starts_at timestamptz,
    ends_at timestamptz,
    timezone text CHECK (char_length(timezone) BETWEEN 1 AND 64),
    base_currency char(3) NOT NULL CHECK (base_currency ~ '^[A-Z]{{3}}$'),
    visibility text NOT NULL CHECK (visibility IN ('group', 'participants')),
    description text CHECK (char_length(description) <= 2000),
    location_label text CHECK (char_length(location_label) <= 200),
    duplicated_from_plan_id uuid REFERENCES plans.plans (id),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    deletion_scheduled_at timestamptz,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (series_id, occurrence_key),
    CHECK ((series_id IS NULL) = (occurrence_key IS NULL)),
    CHECK (series_id IS NOT NULL OR NOT is_series_exception),
    CHECK (visibility = 'participants' OR group_id IS NOT NULL),
    CHECK (
        (timing_mode = 'undecided'
            AND start_date IS NULL AND end_date IS NULL AND starts_at IS NULL AND ends_at IS NULL)
        OR (timing_mode = 'date'
            AND start_date IS NOT NULL AND starts_at IS NULL AND ends_at IS NULL
            AND (end_date IS NULL OR end_date >= start_date))
        OR (timing_mode = 'datetime'
            AND starts_at IS NOT NULL AND timezone IS NOT NULL
            AND start_date IS NULL AND end_date IS NULL
            AND (ends_at IS NULL OR ends_at >= starts_at))
    )
);
CREATE INDEX plans_group_idx ON plans.plans (group_id) WHERE group_id IS NOT NULL;

CREATE TABLE plans.plan_participants (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    identity_kind text NOT NULL CHECK (identity_kind IN ('user', 'guest', 'placeholder')),
    user_id uuid REFERENCES iam.users (id),
    display_name text NOT NULL CHECK (char_length(btrim(display_name)) BETWEEN 1 AND 80),
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer', 'guest')),
    access_state text NOT NULL CHECK (
        access_state IN ('pending_approval', 'active', 'left', 'removed', 'merged')
    ),
    rsvp_status text NOT NULL CHECK (rsvp_status IN ('invited', 'going', 'maybe', 'declined')),
    rsvp_updated_at timestamptz,
    merged_into_participant_id uuid,
    joined_via_invite_id uuid,
    added_by_user_id uuid REFERENCES iam.users (id),
    joined_at timestamptz,
    left_at timestamptz,
    removed_at timestamptz,
    claimed_at timestamptz,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, merged_into_participant_id)
        REFERENCES plans.plan_participants (plan_id, id),
    CHECK ((identity_kind = 'placeholder') = (user_id IS NULL)),
    CHECK ((access_state = 'merged') = (merged_into_participant_id IS NOT NULL)),
    CHECK (merged_into_participant_id IS NULL OR merged_into_participant_id <> id),
    CHECK (role <> 'owner' OR (access_state = 'active' AND identity_kind = 'user'))
);
CREATE UNIQUE INDEX plan_participants_user_once
    ON plans.plan_participants (plan_id, user_id)
    WHERE user_id IS NOT NULL AND access_state <> 'merged';
CREATE UNIQUE INDEX plan_participants_single_owner
    ON plans.plan_participants (plan_id) WHERE role = 'owner';
CREATE INDEX plan_participants_user_idx
    ON plans.plan_participants (user_id, plan_id) WHERE user_id IS NOT NULL;

CREATE TABLE plans.plan_invites (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.plans (id),
    purpose text NOT NULL CHECK (purpose IN ('join', 'claim')),
    target_participant_id uuid,
    token_hash bytea NOT NULL UNIQUE,
    role text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    allow_guests boolean NOT NULL,
    requires_approval boolean NOT NULL,
    intended_email_hash bytea,
    max_uses integer CHECK (max_uses BETWEEN 1 AND 1000),
    use_count integer NOT NULL CHECK (use_count >= 0),
    expires_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'revoked')),
    revoked_at timestamptz,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, target_participant_id)
        REFERENCES plans.plan_participants (plan_id, id),
    CHECK ((purpose = 'claim') = (target_participant_id IS NOT NULL)),
    CHECK (max_uses IS NULL OR use_count <= max_uses),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);
CREATE INDEX plan_invites_plan_idx ON plans.plan_invites (plan_id);

ALTER TABLE plans.plan_participants
    ADD CONSTRAINT plan_participants_invite_fkey
    FOREIGN KEY (plan_id, joined_via_invite_id) REFERENCES plans.plan_invites (plan_id, id);

CREATE TABLE plans.travel_plan_details (
    plan_id uuid PRIMARY KEY REFERENCES plans.plans (id),
    destination_summary text CHECK (char_length(destination_summary) <= 200),
    notes text CHECK (char_length(notes) <= 2000),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE plans.travel_segments (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans.travel_plan_details (plan_id),
    segment_type text NOT NULL CHECK (
        segment_type IN ('flight', 'train', 'bus', 'car', 'ferry', 'lodging', 'other')
    ),
    title text CHECK (char_length(title) <= 120),
    origin_label text CHECK (char_length(origin_label) <= 200),
    destination_label text CHECK (char_length(destination_label) <= 200),
    timing_mode text NOT NULL CHECK (timing_mode IN ('date', 'datetime')),
    start_date date,
    end_date date,
    departure_local timestamp,
    departure_timezone text CHECK (char_length(departure_timezone) BETWEEN 1 AND 64),
    departs_at timestamptz,
    arrival_local timestamp,
    arrival_timezone text CHECK (char_length(arrival_timezone) BETWEEN 1 AND 64),
    arrives_at timestamptz,
    sort_order integer NOT NULL CHECK (sort_order >= 0),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    CHECK (
        (timing_mode = 'date'
            AND start_date IS NOT NULL AND (end_date IS NULL OR end_date >= start_date)
            AND departure_local IS NULL AND departure_timezone IS NULL AND departs_at IS NULL
            AND arrival_local IS NULL AND arrival_timezone IS NULL AND arrives_at IS NULL)
        OR (timing_mode = 'datetime'
            AND start_date IS NULL AND end_date IS NULL
            AND departure_local IS NOT NULL AND departure_timezone IS NOT NULL
            AND departs_at IS NOT NULL
            AND ((arrival_local IS NULL AND arrival_timezone IS NULL AND arrives_at IS NULL)
                OR (arrival_local IS NOT NULL AND arrival_timezone IS NOT NULL
                    AND arrives_at IS NOT NULL AND arrives_at >= departs_at)))
    )
);
CREATE INDEX travel_segments_plan_idx ON plans.travel_segments (plan_id) WHERE deleted_at IS NULL;

CREATE TABLE sync_audit.audit_events (
    id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    actor_user_id uuid,
    actor_session_id uuid,
    request_id text CHECK (char_length(request_id) <= 128),
    action text NOT NULL CHECK (char_length(action) BETWEEN 1 AND 100),
    entity_type text NOT NULL CHECK (char_length(entity_type) BETWEEN 1 AND 64),
    entity_id uuid NOT NULL,
    group_id uuid,
    plan_id uuid,
    metadata jsonb NOT NULL
);
CREATE INDEX audit_events_plan_idx ON sync_audit.audit_events (plan_id, occurred_at)
    WHERE plan_id IS NOT NULL;
CREATE INDEX audit_events_group_idx ON sync_audit.audit_events (group_id, occurred_at)
    WHERE group_id IS NOT NULL;

CREATE TABLE sync_audit.change_log (
    server_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    changed_at timestamptz NOT NULL,
    scope_type text NOT NULL CHECK (scope_type IN ('user', 'group', 'plan')),
    scope_id uuid NOT NULL,
    entity_type text NOT NULL CHECK (char_length(entity_type) BETWEEN 1 AND 64),
    entity_id uuid NOT NULL,
    entity_version integer NOT NULL CHECK (entity_version > 0),
    operation text NOT NULL CHECK (operation IN ('upsert', 'delete')),
    actor_user_id uuid,
    request_id text CHECK (char_length(request_id) <= 128)
);
CREATE INDEX change_log_scope_idx ON sync_audit.change_log (scope_type, scope_id, server_seq);
"""

# Exactly one owner per group/plan is checked at commit so ownership transfer can
# demote and promote inside one transaction.
OWNER_INVARIANT_SQL = """
CREATE FUNCTION groups.enforce_single_owner() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    target uuid;
BEGIN
    IF TG_TABLE_NAME = 'groups' THEN
        target := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target := OLD.group_id;
    ELSE
        target := NEW.group_id;
    END IF;
    IF EXISTS (SELECT 1 FROM groups.groups WHERE id = target)
       AND (SELECT count(*) FROM groups.group_memberships
            WHERE group_id = target AND role = 'owner') <> 1 THEN
        RAISE EXCEPTION 'group % must have exactly one owner', target
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER groups_require_owner
    AFTER INSERT ON groups.groups
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION groups.enforce_single_owner();
CREATE CONSTRAINT TRIGGER group_memberships_require_owner
    AFTER INSERT OR UPDATE OR DELETE ON groups.group_memberships
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION groups.enforce_single_owner();

CREATE FUNCTION plans.enforce_single_owner() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    target uuid;
BEGIN
    IF TG_TABLE_NAME = 'plans' THEN
        target := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target := OLD.plan_id;
    ELSE
        target := NEW.plan_id;
    END IF;
    IF EXISTS (SELECT 1 FROM plans.plans WHERE id = target)
       AND (SELECT count(*) FROM plans.plan_participants
            WHERE plan_id = target AND role = 'owner') <> 1 THEN
        RAISE EXCEPTION 'plan % must have exactly one owner', target
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER plans_require_owner
    AFTER INSERT ON plans.plans
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION plans.enforce_single_owner();
CREATE CONSTRAINT TRIGGER plan_participants_require_owner
    AFTER INSERT OR UPDATE OR DELETE ON plans.plan_participants
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION plans.enforce_single_owner();
"""

# Transaction-local context: app.actor_id (current user) and app.invite_token_hash
# (hex digest of the invite token presented in this request, if any).
POLICY_FUNCTIONS_SQL = """
CREATE FUNCTION iam.actor_id() RETURNS uuid
    LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT NULLIF(current_setting('app.actor_id', true), '')::uuid
$$;

CREATE FUNCTION iam.resolve_user_by_email(p_email text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT id FROM iam.users WHERE email = p_email
$$;

CREATE FUNCTION groups.actor_is_active_member(p_group_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM groups.group_memberships
        WHERE group_id = p_group_id AND user_id = iam.actor_id() AND state = 'active'
    )
$$;

CREATE FUNCTION groups.actor_can_view_group(p_group_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM groups.group_memberships
        WHERE group_id = p_group_id AND user_id = iam.actor_id()
          AND state IN ('active', 'invited')
    )
$$;

CREATE FUNCTION groups.actor_is_unowned_group_creator(p_group_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM groups.groups g
        WHERE g.id = p_group_id AND g.created_by_user_id = iam.actor_id()
          AND NOT EXISTS (SELECT 1 FROM groups.group_memberships m WHERE m.group_id = g.id)
    )
$$;

CREATE FUNCTION plans.actor_is_active_participant(p_plan_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND user_id = iam.actor_id() AND access_state = 'active'
    )
$$;

CREATE FUNCTION plans.actor_has_participant_row(p_plan_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND user_id = iam.actor_id()
    )
$$;

CREATE FUNCTION plans.actor_can_view_plan(p_plan_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT plans.actor_is_active_participant(p_plan_id) OR EXISTS (
        SELECT 1 FROM plans.plans p
        WHERE p.id = p_plan_id AND p.visibility = 'group'
          AND groups.actor_is_active_member(p.group_id)
    )
$$;

CREATE FUNCTION plans.actor_is_unowned_plan_creator(p_plan_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plans p
        WHERE p.id = p_plan_id AND p.created_by_user_id = iam.actor_id()
          AND NOT EXISTS (SELECT 1 FROM plans.plan_participants pp WHERE pp.plan_id = p.id)
    )
$$;

CREATE FUNCTION plans.presented_invite_token_hash() RETURNS bytea
    LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT decode(NULLIF(current_setting('app.invite_token_hash', true), ''), 'hex')
$$;

CREATE FUNCTION groups.actor_holds_invite(p_group_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM groups.group_invites
        WHERE group_id = p_group_id AND state = 'active'
          AND token_hash = plans.presented_invite_token_hash()
    )
$$;

CREATE FUNCTION plans.actor_holds_invite(p_plan_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM plans.plan_invites
        WHERE plan_id = p_plan_id AND state = 'active'
          AND token_hash = plans.presented_invite_token_hash()
    )
$$;

CREATE FUNCTION iam.actor_shares_context(p_user_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
        FROM groups.group_memberships mine
        JOIN groups.group_memberships theirs ON theirs.group_id = mine.group_id
        WHERE mine.user_id = iam.actor_id() AND mine.state = 'active'
          AND theirs.user_id = p_user_id
    ) OR EXISTS (
        SELECT 1
        FROM plans.plan_participants mine
        JOIN plans.plan_participants theirs ON theirs.plan_id = mine.plan_id
        WHERE mine.user_id = iam.actor_id() AND mine.access_state = 'active'
          AND theirs.user_id = p_user_id
    )
$$;
"""

RLS_SQL = """
ALTER TABLE iam.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam.user_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam.refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam.email_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam.rate_limit_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE groups.groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE groups.group_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE groups.group_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE plans.plan_series ENABLE ROW LEVEL SECURITY;
ALTER TABLE plans.plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE plans.plan_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE plans.plan_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE plans.travel_plan_details ENABLE ROW LEVEL SECURITY;
ALTER TABLE plans.travel_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_audit.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_audit.change_log ENABLE ROW LEVEL SECURITY;

-- Profiles: self, or people the actor currently shares a group or plan with.
CREATE POLICY users_select ON iam.users FOR SELECT TO api_runtime, worker_runtime
    USING (id = iam.actor_id() OR iam.actor_shares_context(id));
CREATE POLICY users_insert ON iam.users FOR INSERT TO api_runtime
    WITH CHECK (id = iam.actor_id());
CREATE POLICY users_update ON iam.users FOR UPDATE TO api_runtime
    USING (id = iam.actor_id()) WITH CHECK (id = iam.actor_id());

-- Credential tables are reached only by authentication flows that run before an
-- actor exists. They are never exposed through tenant queries; access is limited
-- by grants to the API (and the worker for delivery and expiry purges).
CREATE POLICY user_identities_api ON iam.user_identities FOR ALL TO api_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY sessions_api ON iam.sessions FOR ALL TO api_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY refresh_tokens_api ON iam.refresh_tokens FOR ALL TO api_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY refresh_tokens_worker ON iam.refresh_tokens FOR ALL TO worker_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY email_challenges_api ON iam.email_challenges FOR ALL TO api_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY email_challenges_worker ON iam.email_challenges FOR ALL TO worker_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY rate_limit_counters_api ON iam.rate_limit_counters FOR ALL TO api_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY rate_limit_counters_worker ON iam.rate_limit_counters FOR ALL TO worker_runtime
    USING (true) WITH CHECK (true);

CREATE POLICY groups_select ON groups.groups FOR SELECT TO api_runtime, worker_runtime
    USING (groups.actor_can_view_group(id) OR groups.actor_holds_invite(id));
CREATE POLICY groups_insert ON groups.groups FOR INSERT TO api_runtime
    WITH CHECK (created_by_user_id = iam.actor_id());
CREATE POLICY groups_update ON groups.groups FOR UPDATE TO api_runtime
    USING (groups.actor_is_active_member(id))
    WITH CHECK (groups.actor_is_active_member(id));

CREATE POLICY group_memberships_select ON groups.group_memberships
    FOR SELECT TO api_runtime, worker_runtime
    USING (user_id = iam.actor_id() OR groups.actor_is_active_member(group_id));
CREATE POLICY group_memberships_insert ON groups.group_memberships FOR INSERT TO api_runtime
    WITH CHECK (
        groups.actor_is_active_member(group_id)
        OR (user_id = iam.actor_id()
            AND (groups.actor_is_unowned_group_creator(group_id)
                OR groups.actor_holds_invite(group_id)))
    );
CREATE POLICY group_memberships_update ON groups.group_memberships FOR UPDATE TO api_runtime
    USING (user_id = iam.actor_id() OR groups.actor_is_active_member(group_id))
    WITH CHECK (user_id = iam.actor_id() OR groups.actor_is_active_member(group_id));

CREATE POLICY group_invites_select ON groups.group_invites FOR SELECT TO api_runtime
    USING (
        groups.actor_is_active_member(group_id)
        OR token_hash = plans.presented_invite_token_hash()
    );
CREATE POLICY group_invites_insert ON groups.group_invites FOR INSERT TO api_runtime
    WITH CHECK (groups.actor_is_active_member(group_id));
CREATE POLICY group_invites_update ON groups.group_invites FOR UPDATE TO api_runtime
    USING (
        groups.actor_is_active_member(group_id)
        OR token_hash = plans.presented_invite_token_hash()
    )
    WITH CHECK (
        groups.actor_is_active_member(group_id)
        OR token_hash = plans.presented_invite_token_hash()
    );

CREATE POLICY plan_series_select ON plans.plan_series FOR SELECT TO api_runtime
    USING (
        created_by_user_id = iam.actor_id()
        OR (group_id IS NOT NULL AND groups.actor_is_active_member(group_id))
    );
-- The horizon job enumerates active series, then acts as each series creator.
CREATE POLICY plan_series_select_worker ON plans.plan_series FOR SELECT TO worker_runtime
    USING (true);
CREATE POLICY plan_series_insert ON plans.plan_series FOR INSERT TO api_runtime
    WITH CHECK (
        created_by_user_id = iam.actor_id()
        AND (group_id IS NULL OR groups.actor_is_active_member(group_id))
    );
CREATE POLICY plan_series_update ON plans.plan_series FOR UPDATE TO api_runtime, worker_runtime
    USING (
        created_by_user_id = iam.actor_id()
        OR (group_id IS NOT NULL AND groups.actor_is_active_member(group_id))
    )
    WITH CHECK (
        created_by_user_id = iam.actor_id()
        OR (group_id IS NOT NULL AND groups.actor_is_active_member(group_id))
    );

CREATE POLICY plans_select ON plans.plans FOR SELECT TO api_runtime, worker_runtime
    USING (plans.actor_can_view_plan(id) OR plans.actor_holds_invite(id));
CREATE POLICY plans_insert ON plans.plans FOR INSERT TO api_runtime, worker_runtime
    WITH CHECK (
        created_by_user_id = iam.actor_id()
        AND (group_id IS NULL OR groups.actor_is_active_member(group_id))
    );
CREATE POLICY plans_update ON plans.plans FOR UPDATE TO api_runtime, worker_runtime
    USING (plans.actor_is_active_participant(id))
    WITH CHECK (plans.actor_is_active_participant(id));

CREATE POLICY plan_participants_select ON plans.plan_participants
    FOR SELECT TO api_runtime, worker_runtime
    USING (
        user_id = iam.actor_id()
        OR plans.actor_can_view_plan(plan_id)
        OR plans.actor_holds_invite(plan_id)
    );
CREATE POLICY plan_participants_insert ON plans.plan_participants
    FOR INSERT TO api_runtime, worker_runtime
    WITH CHECK (
        plans.actor_is_active_participant(plan_id)
        OR plans.actor_is_unowned_plan_creator(plan_id)
        OR (user_id = iam.actor_id()
            AND (plans.actor_holds_invite(plan_id) OR plans.actor_can_view_plan(plan_id)))
    );
-- Rows stay inside plans where the actor already has a row or holds an invite;
-- guest-account claims relink the actor's own rows to the claiming user.
CREATE POLICY plan_participants_update ON plans.plan_participants
    FOR UPDATE TO api_runtime, worker_runtime
    USING (
        user_id = iam.actor_id()
        OR plans.actor_is_active_participant(plan_id)
        OR plans.actor_holds_invite(plan_id)
    )
    WITH CHECK (
        plans.actor_has_participant_row(plan_id) OR plans.actor_holds_invite(plan_id)
    );

CREATE POLICY plan_invites_select ON plans.plan_invites FOR SELECT TO api_runtime
    USING (
        plans.actor_is_active_participant(plan_id)
        OR token_hash = plans.presented_invite_token_hash()
    );
CREATE POLICY plan_invites_insert ON plans.plan_invites FOR INSERT TO api_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY plan_invites_update ON plans.plan_invites FOR UPDATE TO api_runtime
    USING (
        plans.actor_is_active_participant(plan_id)
        OR token_hash = plans.presented_invite_token_hash()
    )
    WITH CHECK (
        plans.actor_is_active_participant(plan_id)
        OR token_hash = plans.presented_invite_token_hash()
    );

CREATE POLICY travel_plan_details_select ON plans.travel_plan_details
    FOR SELECT TO api_runtime, worker_runtime
    USING (plans.actor_can_view_plan(plan_id));
CREATE POLICY travel_plan_details_insert ON plans.travel_plan_details
    FOR INSERT TO api_runtime, worker_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY travel_plan_details_update ON plans.travel_plan_details
    FOR UPDATE TO api_runtime, worker_runtime
    USING (plans.actor_is_active_participant(plan_id))
    WITH CHECK (plans.actor_is_active_participant(plan_id));

CREATE POLICY travel_segments_select ON plans.travel_segments
    FOR SELECT TO api_runtime, worker_runtime
    USING (plans.actor_can_view_plan(plan_id));
CREATE POLICY travel_segments_insert ON plans.travel_segments
    FOR INSERT TO api_runtime, worker_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY travel_segments_update ON plans.travel_segments
    FOR UPDATE TO api_runtime, worker_runtime
    USING (plans.actor_is_active_participant(plan_id))
    WITH CHECK (plans.actor_is_active_participant(plan_id));

-- Append-only: runtime roles may insert but never read, update, or delete here.
CREATE POLICY audit_events_insert ON sync_audit.audit_events
    FOR INSERT TO api_runtime, worker_runtime WITH CHECK (true);
CREATE POLICY change_log_insert ON sync_audit.change_log
    FOR INSERT TO api_runtime, worker_runtime WITH CHECK (true);
"""

GRANTS_SQL = """
GRANT SELECT, INSERT, UPDATE ON
    iam.users, iam.user_identities, iam.sessions, iam.refresh_tokens,
    iam.email_challenges, iam.rate_limit_counters
    TO api_runtime;
GRANT SELECT ON iam.users TO worker_runtime;
GRANT SELECT, UPDATE, DELETE ON iam.email_challenges, iam.refresh_tokens TO worker_runtime;
GRANT SELECT, DELETE ON iam.rate_limit_counters TO worker_runtime;

GRANT SELECT, INSERT, UPDATE ON
    groups.groups, groups.group_memberships, groups.group_invites,
    plans.plan_series, plans.plans, plans.plan_participants, plans.plan_invites,
    plans.travel_plan_details, plans.travel_segments
    TO api_runtime;
GRANT SELECT ON groups.groups, groups.group_memberships TO worker_runtime;
GRANT SELECT, INSERT, UPDATE ON
    plans.plan_series, plans.plans, plans.plan_participants,
    plans.travel_plan_details, plans.travel_segments
    TO worker_runtime;

GRANT INSERT ON sync_audit.audit_events, sync_audit.change_log TO api_runtime, worker_runtime;
GRANT USAGE ON SEQUENCE sync_audit.change_log_server_seq_seq TO api_runtime, worker_runtime;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA iam, groups, plans FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    iam.actor_id(),
    iam.actor_shares_context(uuid),
    groups.actor_is_active_member(uuid),
    groups.actor_can_view_group(uuid),
    groups.actor_is_unowned_group_creator(uuid),
    plans.actor_is_active_participant(uuid),
    plans.actor_has_participant_row(uuid),
    plans.actor_can_view_plan(uuid),
    plans.actor_is_unowned_plan_creator(uuid),
    plans.presented_invite_token_hash(),
    plans.actor_holds_invite(uuid),
    groups.actor_holds_invite(uuid)
    TO api_runtime, worker_runtime;
GRANT EXECUTE ON FUNCTION iam.resolve_user_by_email(text) TO api_runtime;
"""


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(OWNER_INVARIANT_SQL)
    op.execute(POLICY_FUNCTIONS_SQL)
    op.execute(RLS_SQL)
    op.execute(GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("Identity, group, and plan tables are forward-only")
