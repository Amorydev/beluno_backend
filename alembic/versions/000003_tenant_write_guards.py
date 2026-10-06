"""Write guards that make RLS hold against insiders, not only outsiders.

Revision ID: 000003_tenant_write_guards
Revises: 000002_identity_groups_plans
Create Date: 2026-10-06

RLS policies decide which rows a runtime role may touch; these BEFORE triggers
decide what an insider may change on them. For api_runtime/worker_runtime:

* tenant keys are immutable (participant/membership/invite/plan/series scope);
* only an active owner/admin may change other people's rows or the plan/group;
* only the current owner may move the owner role;
* a non-manager may change only their own row, through the transitions the API
  offers: RSVP, leaving, guest upgrade/claim, merge, and rejoining with a held
  invite (or, for a group-visible plan, as an active group member);
* an invite-token holder may only count one use of that invite.

Forward action: new SECURITY DEFINER helpers and BEFORE triggers only.
Lock/scan risk: none. Validation: ``SELECT tgname FROM pg_trigger WHERE tgname LIKE
'%write_guard'`` returns seven rows. Compatibility: the API already performs only
these transitions. Rollback: forward-only; drop a trigger in a follow-up revision
if it ever blocks a legitimate flow. Maintenance roles (migrator, superuser) are
not guarded, matching their RLS owner bypass.
"""

from alembic import op

revision = "000003_tenant_write_guards"
down_revision = "000002_identity_groups_plans"
branch_labels = None
depends_on = None

HELPERS_SQL = """
CREATE FUNCTION plans.actor_plan_role(p_plan_id uuid) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT role FROM plans.plan_participants
    WHERE plan_id = p_plan_id AND user_id = iam.actor_id() AND access_state = 'active'
$$;

CREATE FUNCTION plans.actor_held_invite_role(p_plan_id uuid) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT role FROM plans.plan_invites
    WHERE plan_id = p_plan_id AND state = 'active'
      AND token_hash = plans.presented_invite_token_hash()
$$;

CREATE FUNCTION plans.actor_held_claim_target(p_plan_id uuid) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT target_participant_id FROM plans.plan_invites
    WHERE plan_id = p_plan_id AND state = 'active' AND purpose = 'claim'
      AND token_hash = plans.presented_invite_token_hash()
$$;

CREATE FUNCTION groups.actor_group_role(p_group_id uuid) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT role FROM groups.group_memberships
    WHERE group_id = p_group_id AND user_id = iam.actor_id() AND state = 'active'
$$;

CREATE FUNCTION groups.actor_held_invite_role(p_group_id uuid) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT role FROM groups.group_invites
    WHERE group_id = p_group_id AND state = 'active'
      AND token_hash = plans.presented_invite_token_hash()
$$;

-- True only after the current owner demoted themselves earlier in this transaction
-- (the first half of an ownership transfer); a commit-time trigger still requires
-- exactly one owner, and non-owners can never vacate the owner seat.
CREATE FUNCTION plans.owner_seat_vacant(p_plan_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM plans.plan_participants WHERE plan_id = p_plan_id AND role = 'owner'
    )
$$;

CREATE FUNCTION groups.owner_seat_vacant(p_group_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM groups.group_memberships WHERE group_id = p_group_id AND role = 'owner'
    )
$$;

CREATE FUNCTION iam.is_guarded_runtime() RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT current_user IN ('api_runtime', 'worker_runtime')
$$;
"""

GUARDS_SQL = """
CREATE FUNCTION plans.participant_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    actor_role text;
    invite_role text;
    claim_target uuid;
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.id <> OLD.id OR NEW.plan_id <> OLD.plan_id) THEN
        RAISE EXCEPTION 'participant scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    actor_role := plans.actor_plan_role(NEW.plan_id);
    IF TG_OP = 'UPDATE' AND (NEW.role = 'owner') <> (OLD.role = 'owner')
       AND actor_role IS DISTINCT FROM 'owner'
       AND NOT (NEW.role = 'owner' AND actor_role = 'admin'
                AND plans.owner_seat_vacant(NEW.plan_id)) THEN
        RAISE EXCEPTION 'only the owner can move ownership' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF actor_role IN ('owner', 'admin')
       OR (TG_OP = 'INSERT' AND plans.actor_is_unowned_plan_creator(NEW.plan_id)) THEN
        RETURN NEW;
    END IF;
    invite_role := plans.actor_held_invite_role(NEW.plan_id);
    IF TG_OP = 'INSERT' THEN
        IF NEW.user_id IS DISTINCT FROM actor
           OR NEW.role NOT IN ('member', 'guest', coalesce(invite_role, 'member'))
           OR NEW.access_state NOT IN ('active', 'pending_approval') THEN
            RAISE EXCEPTION 'participant insert not allowed'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    claim_target := plans.actor_held_claim_target(NEW.plan_id);
    IF OLD.user_id IS DISTINCT FROM actor
       AND NOT (OLD.id = claim_target AND OLD.identity_kind = 'placeholder') THEN
        RAISE EXCEPTION 'participant update not allowed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.role <> OLD.role
       AND NEW.role NOT IN ('member', 'guest', coalesce(invite_role, 'member')) THEN
        RAISE EXCEPTION 'participant role change not allowed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.access_state <> OLD.access_state AND NOT (
        NEW.access_state IN ('left', 'merged')
        OR (OLD.access_state = 'left'
            AND NEW.access_state IN ('active', 'pending_approval')
            AND (invite_role IS NOT NULL OR plans.actor_can_view_plan(NEW.plan_id)))
    ) THEN
        RAISE EXCEPTION 'participant access change not allowed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.user_id IS DISTINCT FROM OLD.user_id
       AND OLD.identity_kind NOT IN ('guest', 'placeholder') THEN
        RAISE EXCEPTION 'participant identity change not allowed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION plans.plan_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id
       OR NEW.group_id IS DISTINCT FROM OLD.group_id
       OR NEW.series_id IS DISTINCT FROM OLD.series_id
       OR NEW.occurrence_key IS DISTINCT FROM OLD.occurrence_key
       OR NEW.created_by_user_id <> OLD.created_by_user_id THEN
        RAISE EXCEPTION 'plan scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF plans.actor_plan_role(NEW.id) IS DISTINCT FROM 'owner'
       AND plans.actor_plan_role(NEW.id) IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'plan update not allowed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION plans.invite_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.plan_id <> OLD.plan_id OR NEW.token_hash <> OLD.token_hash
       OR NEW.purpose <> OLD.purpose
       OR NEW.target_participant_id IS DISTINCT FROM OLD.target_participant_id
       OR NEW.created_by_user_id <> OLD.created_by_user_id THEN
        RAISE EXCEPTION 'invite scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF plans.actor_plan_role(NEW.plan_id) IN ('owner', 'admin') THEN
        RETURN NEW;
    END IF;
    IF (NEW.role, NEW.allow_guests, NEW.requires_approval, NEW.intended_email_hash,
        NEW.max_uses, NEW.expires_at, NEW.state, NEW.revoked_at)
       IS DISTINCT FROM
       (OLD.role, OLD.allow_guests, OLD.requires_approval, OLD.intended_email_hash,
        OLD.max_uses, OLD.expires_at, OLD.state, OLD.revoked_at)
       OR NEW.use_count <> OLD.use_count + 1 THEN
        RAISE EXCEPTION 'invite holders may only count one use'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION groups.group_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.created_by_user_id <> OLD.created_by_user_id THEN
        RAISE EXCEPTION 'group scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF groups.actor_group_role(NEW.id) IS DISTINCT FROM 'owner'
       AND groups.actor_group_role(NEW.id) IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'group update not allowed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION groups.membership_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    actor_role text;
    invite_role text;
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.group_id <> OLD.group_id OR NEW.user_id <> OLD.user_id) THEN
        RAISE EXCEPTION 'membership scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    actor_role := groups.actor_group_role(NEW.group_id);
    IF TG_OP = 'UPDATE' AND (NEW.role = 'owner') <> (OLD.role = 'owner')
       AND actor_role IS DISTINCT FROM 'owner'
       AND NOT (NEW.role = 'owner' AND actor_role = 'admin'
                AND groups.owner_seat_vacant(NEW.group_id)) THEN
        RAISE EXCEPTION 'only the owner can move ownership' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF actor_role IN ('owner', 'admin')
       OR (TG_OP = 'INSERT' AND groups.actor_is_unowned_group_creator(NEW.group_id)) THEN
        RETURN NEW;
    END IF;
    invite_role := groups.actor_held_invite_role(NEW.group_id);
    IF TG_OP = 'INSERT' THEN
        IF NEW.user_id IS DISTINCT FROM actor OR NEW.state <> 'active'
           OR NEW.role IS DISTINCT FROM invite_role THEN
            RAISE EXCEPTION 'membership insert not allowed'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.user_id IS DISTINCT FROM actor
       OR (NEW.role <> OLD.role AND NEW.role IS DISTINCT FROM invite_role)
       OR (NEW.state <> OLD.state AND NOT (
            (OLD.state = 'invited' AND NEW.state IN ('active', 'left'))
            OR (OLD.state = 'active' AND NEW.state = 'left')
            OR (OLD.state = 'left' AND NEW.state = 'active' AND invite_role IS NOT NULL))) THEN
        RAISE EXCEPTION 'membership update not allowed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION groups.group_invite_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.group_id <> OLD.group_id OR NEW.token_hash <> OLD.token_hash
       OR NEW.created_by_user_id <> OLD.created_by_user_id THEN
        RAISE EXCEPTION 'invite scope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF groups.actor_group_role(NEW.group_id) IN ('owner', 'admin') THEN
        RETURN NEW;
    END IF;
    IF (NEW.role, NEW.max_uses, NEW.expires_at, NEW.state, NEW.revoked_at)
       IS DISTINCT FROM (OLD.role, OLD.max_uses, OLD.expires_at, OLD.state, OLD.revoked_at)
       OR NEW.use_count <> OLD.use_count + 1 THEN
        RAISE EXCEPTION 'invite holders may only count one use'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION plans.series_write_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT iam.is_guarded_runtime() THEN
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.group_id IS DISTINCT FROM OLD.group_id
       OR NEW.created_by_user_id <> OLD.created_by_user_id
       OR OLD.created_by_user_id IS DISTINCT FROM iam.actor_id() THEN
        RAISE EXCEPTION 'only the series creator can change it'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER plan_participants_write_guard
    BEFORE INSERT OR UPDATE ON plans.plan_participants
    FOR EACH ROW EXECUTE FUNCTION plans.participant_write_guard();
CREATE TRIGGER plans_write_guard
    BEFORE UPDATE ON plans.plans
    FOR EACH ROW EXECUTE FUNCTION plans.plan_write_guard();
CREATE TRIGGER plan_invites_write_guard
    BEFORE UPDATE ON plans.plan_invites
    FOR EACH ROW EXECUTE FUNCTION plans.invite_write_guard();
CREATE TRIGGER plan_series_write_guard
    BEFORE UPDATE ON plans.plan_series
    FOR EACH ROW EXECUTE FUNCTION plans.series_write_guard();
CREATE TRIGGER groups_write_guard
    BEFORE UPDATE ON groups.groups
    FOR EACH ROW EXECUTE FUNCTION groups.group_write_guard();
CREATE TRIGGER group_memberships_write_guard
    BEFORE INSERT OR UPDATE ON groups.group_memberships
    FOR EACH ROW EXECUTE FUNCTION groups.membership_write_guard();
CREATE TRIGGER group_invites_write_guard
    BEFORE UPDATE ON groups.group_invites
    FOR EACH ROW EXECUTE FUNCTION groups.group_invite_write_guard();
"""

GRANTS_SQL = """
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA iam, groups, plans FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    plans.actor_plan_role(uuid),
    plans.actor_held_invite_role(uuid),
    plans.actor_held_claim_target(uuid),
    groups.actor_group_role(uuid),
    groups.actor_held_invite_role(uuid),
    plans.owner_seat_vacant(uuid),
    groups.owner_seat_vacant(uuid),
    iam.is_guarded_runtime()
    TO api_runtime, worker_runtime;
-- Functions created later by the migrator are not executable by PUBLIC either.
ALTER DEFAULT PRIVILEGES FOR ROLE migrator REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(HELPERS_SQL)
    op.execute(GUARDS_SQL)
    op.execute(GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("Tenant write guards are forward-only")
