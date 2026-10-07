"""Passkeys (WebAuthn) for signing in and stepping up.

Revision ID: 000016_passkeys
Revises: 000015_problem_reports
Create Date: 2026-10-07

Forward action:

* ``iam.passkeys``: a registered account's WebAuthn credentials (credential ID,
  COSE public key, signature counter, transports, backup state, a label). Only
  registered accounts add them, after a recent sign-in; they sign in and step up.
  No attestation is kept.
* ``iam.webauthn_challenges``: single-use challenges (stored as an HMAC digest)
  for registering a passkey or signing in with one, valid for minutes.
* Both are credential tables like ``iam.user_identities``: reached by
  authentication flows that run before an actor exists, so reading and updating
  them is open to the API; a passkey is inserted and deleted only by its owner, and
  only its counter, backup state, last use, and label can change. The worker
  purges expired challenges.
* ``iam.sessions.auth_method`` also accepts ``passkey``.
* ``iam.forget_actor_credentials`` also deletes the actor's passkeys and challenges.

Lock/scan risk: two new tables. Replacing the ``auth_method`` check takes an ACCESS
EXCLUSIVE lock on ``iam.sessions`` that the migration's single transaction holds
through the validating scan (no rewrite; the table is small: one row per device).

Validation:
    SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'sessions_auth_method_check';                 -- includes 'passkey'
    SELECT has_table_privilege('api_runtime', 'iam.passkeys', 'DELETE');   -- t

Compatibility: additive; existing sessions keep their methods.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000016_passkeys"
down_revision = "000015_problem_reports"
branch_labels = None
depends_on = None

TABLES_SQL = """
CREATE TABLE iam.passkeys (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES iam.users (id),
    credential_id bytea NOT NULL UNIQUE
        CHECK (octet_length(credential_id) BETWEEN 16 AND 1023),
    public_key bytea NOT NULL CHECK (octet_length(public_key) BETWEEN 1 AND 2048),
    sign_count bigint NOT NULL CHECK (sign_count >= 0),
    transports text[] NOT NULL,
    label text NOT NULL CHECK (char_length(btrim(label)) BETWEEN 1 AND 64),
    backed_up boolean NOT NULL,
    created_at timestamptz NOT NULL,
    last_used_at timestamptz
);
CREATE INDEX passkeys_user_idx ON iam.passkeys (user_id);

CREATE TABLE iam.webauthn_challenges (
    id uuid PRIMARY KEY,
    purpose text NOT NULL CHECK (purpose IN ('register', 'authenticate')),
    user_id uuid REFERENCES iam.users (id),
    challenge_hash bytea NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL,
    CHECK (purpose = 'authenticate' OR user_id IS NOT NULL)
);
CREATE INDEX webauthn_challenges_expiry_idx ON iam.webauthn_challenges (expires_at);

ALTER TABLE iam.passkeys ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam.webauthn_challenges ENABLE ROW LEVEL SECURITY;
-- Credential tables: open to the API's authentication flows, limited by grants.
-- Signing in reads and updates a passkey before any actor exists; adding and removing
-- one are the owner's alone, and updates touch only the counter, backup state, last
-- use, and label.
CREATE POLICY passkeys_select ON iam.passkeys FOR SELECT TO api_runtime USING (true);
CREATE POLICY passkeys_update ON iam.passkeys FOR UPDATE TO api_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY passkeys_insert ON iam.passkeys FOR INSERT TO api_runtime
    WITH CHECK (user_id = iam.actor_id());
CREATE POLICY passkeys_delete ON iam.passkeys FOR DELETE TO api_runtime
    USING (user_id = iam.actor_id());
CREATE POLICY webauthn_challenges_api ON iam.webauthn_challenges FOR ALL TO api_runtime
    USING (true) WITH CHECK (true);
CREATE POLICY webauthn_challenges_worker ON iam.webauthn_challenges FOR ALL TO worker_runtime
    USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, DELETE ON iam.passkeys TO api_runtime;
GRANT UPDATE (sign_count, backed_up, last_used_at, label) ON iam.passkeys TO api_runtime;
GRANT SELECT, INSERT, UPDATE ON iam.webauthn_challenges TO api_runtime;
GRANT SELECT, DELETE ON iam.webauthn_challenges TO worker_runtime;

ALTER TABLE iam.sessions DROP CONSTRAINT sessions_auth_method_check;
ALTER TABLE iam.sessions ADD CONSTRAINT sessions_auth_method_check
    CHECK (auth_method IN ('google', 'apple', 'email', 'guest_invite', 'passkey')) NOT VALID;
ALTER TABLE iam.sessions VALIDATE CONSTRAINT sessions_auth_method_check;
"""

FORGET_SQL = """
-- The acting user's sign-in identities, passkeys, and pending challenges go; the
-- API holds no DELETE grant on most of these, and this gate touches only the actor's.
CREATE OR REPLACE FUNCTION iam.forget_actor_credentials() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := iam.actor_id();
    actor_email text;
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'an actor is required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT email INTO actor_email FROM iam.users WHERE id = actor;
    DELETE FROM iam.user_identities WHERE user_id = actor;
    DELETE FROM iam.passkeys WHERE user_id = actor;
    DELETE FROM iam.webauthn_challenges WHERE user_id = actor;
    IF actor_email IS NOT NULL THEN
        DELETE FROM iam.email_challenges WHERE email = actor_email;
    END IF;
END;
$$;
REVOKE EXECUTE ON FUNCTION iam.forget_actor_credentials() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.forget_actor_credentials() TO api_runtime;
"""


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(FORGET_SQL)


def downgrade() -> None:
    raise RuntimeError("Passkeys are forward-only")
