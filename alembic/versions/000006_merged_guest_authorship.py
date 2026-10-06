"""Merged guest authorship: let an account act on records its claimed guest account made.

Revision ID: 000006_merged_guest_authorship
Revises: 000005_financial_ledger
Create Date: 2026-10-06

Forward action: adds ``iam.user_merged_into_actor(uuid)``, a SECURITY DEFINER
check that the given user is a guest account merged into the acting user
(``iam.users.merged_into_user_id``). Once a guest's participations move to the
claiming account, the guest's profile row usually shares no group or plan with
that account and RLS hides it, yet records the guest made (expenses,
settlements) still name it as creator or recorder. Authorization asks through
this gate instead of widening the ``users`` policy. It returns only a boolean
about the caller's own merged accounts.

Lock/scan risk: none; creates one function.

Validation:
    SELECT has_function_privilege('api_runtime', 'iam.user_merged_into_actor(uuid)', 'EXECUTE');
        -- t
    SELECT has_function_privilege('worker_runtime', 'iam.user_merged_into_actor(uuid)', 'EXECUTE');
        -- f

Compatibility: additive; the previous build never calls the function.

Rollback: forward-only. The previous build ignores the function; drop it in a
later migration if it is ever retired.
"""

from alembic import op

revision = "000006_merged_guest_authorship"
down_revision = "000005_financial_ledger"
branch_labels = None
depends_on = None

FUNCTION_SQL = """
CREATE FUNCTION iam.user_merged_into_actor(p_user_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM iam.users
        WHERE id = p_user_id AND merged_into_user_id = iam.actor_id()
    )
$$;

REVOKE EXECUTE ON FUNCTION iam.user_merged_into_actor(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.user_merged_into_actor(uuid) TO api_runtime;
"""


def upgrade() -> None:
    op.execute(FUNCTION_SQL)


def downgrade() -> None:
    raise RuntimeError("Merged guest authorship is forward-only")
