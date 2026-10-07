# Passkeys (WebAuthn) security review

Branch `feat/passkeys`, uncommitted diff + untracked files. Read-only review; live probes ran on
Testcontainers PG via a scratch pytest file (not committed). py_webauthn 3.0.1 source inspected.

## Scope
- Files: `alembic/versions/000016_passkeys.py`, `src/beluno/modules/iam/passkeys.py`,
  `src/beluno/api/routers/passkeys.py`, `src/beluno/contracts/passkeys.py`, `db/models/iam.py`,
  `config.py`, `iam/{users,external_identity,maintenance,rate_limits}.py`, testkit, tests, docs, deploy.
- LOC: ~1,050 new + ~300 changed (excluding openapi/uv.lock).
- Gates run: `pytest tests/integration/test_passkeys.py tests/unit/test_config.py
  tests/security/test_rls_isolation.py tests/integration/test_worker_jobs.py` 25 passed;
  `ruff check` clean; `mypy src` clean; `openapi.json` matches a fresh export.

## Overall
Core cryptographic path is sound (lib verifies challenge, type, origin list, RP ID hash, UP+UV,
counter, signature; app does lookup by rawId, userHandle check, stores authData credential ID,
HMAC challenge with `compare_digest`, `FOR UPDATE` on challenge and passkey). A passkey cannot
create an account or link one, and cannot authenticate as anyone but its row's `user_id`.
One real invariant break (challenge single-use on `authenticate` errors) with a proven
cross-caller replay, plus several unhandled library exceptions that 500.

## Critical
None.

## High

### H1. Challenge and counter roll back when `authenticate` raises; the assertion stays replayable, including by a signed-out caller
- `src/beluno/api/routers/passkeys.py:132-143` and `src/beluno/modules/iam/passkeys.py:180-214`.
- `verify()` marks the challenge used and advances `sign_count` in the request transaction; any
  `BelunoError` from `authenticate` (`PARTICIPANT_MERGE_REQUIRED` 409, `REAUTHENTICATION_MISMATCH`
  403, inactive-account 401) rolls the whole `open_context` transaction back. Guest challenges are
  issued with `user_id = NULL` (`passkeys.py:153,181`), the same binding as signed-out challenges.
- Probe (live): guest joins a plan whose owner has a passkey, uses the owner's passkey ->
  `409 PARTICIPANT_MERGE_REQUIRED`; DB afterwards: `consumed_at = NULL`, `sign_count = 0`. Replaying
  the identical assertion with **no Authorization header** -> `200` with a full session (access +
  refresh) for the owner. The counter check does not help because the counter rolled back too.
- Impact: the ADR amendment and module docstring promise "used up even when a response fails";
  false for the most common guest-claim path (409 is the expected first answer whenever the account
  already participates). A captured assertion (compromised client, TLS-terminating proxy log) is a
  5-minute bearer credential, transferable from a guest context to a fresh sign-in.
- Fix: (1) run `authenticate` inside `ctx.savepoint()`, catch `BelunoError`, let the outer
  transaction commit the consumed challenge and the new counter, then re-raise after the
  `async with`, mirroring the `None`-then-commit pattern; same for `register`'s 409 path
  (`passkeys.py:139-144`). (2) Bind guest challenges to the guest's `user_id` (the CHECK already
  allows a user on `authenticate`), so a guest's assertion never works for another caller.
- Trade-off (product call): after (1) a guest who gets `PARTICIPANT_MERGE_REQUIRED` must run a
  second biometric ceremony with `merge_guest_participations: true`; today the client can
  silently resend the same assertion. Options: accept the second prompt; or let the client send
  `merge_guest_participations` up front from UI copy shown before the ceremony. Do not keep the
  replayable assertion.
- Add a test: 409 merge-required, then the same assertion signed-out -> 401, and challenge
  `consumed_at IS NOT NULL`.

## Medium

### M1. Library exceptions outside `MALFORMED` escape as 500
- `passkeys.py:67,124,209`. Only `InvalidRegistrationResponse`/`InvalidAuthenticationResponse` +
  `InvalidJSONStructure`, `InvalidCBORData`, `KeyError`, `TypeError`, `ValueError` are caught. py_webauthn
  raises bare `InvalidBackupFlags`, `UnsupportedPublicKeyType`, `InvalidPublicKeyStructure`,
  `UnsupportedEC2Curve`, `UnsupportedPublicKey`, `UnsupportedAlgorithm` unwrapped.
- Probes (registration, any fresh signed-in user): BS flag without BE -> 500 `InvalidBackupFlags`;
  COSE `kty=4` -> 500 `UnsupportedPublicKeyType`. A stored key with an unsupported curve (`crv=99`,
  decodes fine at registration) makes every later sign-in with that credential 500 via
  `decoded_public_key_to_cryptography`.
- Fix: catch `webauthn.helpers.exceptions.WebAuthnException` (base class) in both places; at
  registration also call `decoded_public_key_to_cryptography(decode_credential_public_key(...))`
  before storing, so an unusable key is refused with 422 instead of poisoning the row.

### M2. Unvalidated `response.transports` 500s registration
- `passkeys.py:126,133`: `transports` comes from the raw client dict after verification (py_webauthn
  does not type-check it). Probes: `transports: 5` -> 500 `TypeError: 'int' object is not iterable`;
  `[["x"]]` -> 500 `unhashable type: 'list'`. Signed-in user, 10/hour cap, but it pages and the
  challenge rolls back.
- Fix: `raw = response.get("transports"); transports = [t for t in raw if isinstance(t, str) and t in
  KNOWN_TRANSPORTS] if isinstance(raw, list) else []`, or take `verified`/parsed credential
  transports.

### M3. Sign-in rate limit is shared by options and verify, and keyed on full IP
- `routers/passkeys.py:113-115,127-129`, `rate_limits.py:44`: one ceremony costs 2 hits on
  `passkey:client` (30/600 s) -> 15 passkey sign-ins/step-ups per 10 min per IP. Behind carrier
  CGNAT or an office NAT this locks out legitimate users (Google/Apple get 60/600 per call).
  Conversely `client_subject` uses the full address, so an IPv6 attacker rotating within a /64 gets
  unbounded unauthenticated `webauthn_challenges` rows (purged only 1 day after expiry).
- Fix: separate buckets (options generous, e.g. 60/600; verify 30/600), and/or aggregate IPv6 to /64
  in `client_subject`. Optional: make signed-out options stateless (HMAC-sealed challenge + expiry,
  insert only on consume) to remove the unauthenticated write.

## Low

- **L1 Vacuous assertion**: `tests/integration/test_passkeys.py:152-156` "A failed response still
  used up its challenge" passes for the wrong reason. After `attempt(sign_count=1)` the soft counter
  is 1, line 154 makes it 2, line 156 signs 3; the server counter is 3, so `3 <= 3` fails the counter
  check regardless of challenge consumption. Use `SoftAuthenticator(counts=False)` or a fresh device
  for this block, or assert `consumed_at IS NOT NULL` via `admin`.
- **L2 CHECK violations reported as duplicates**: `passkeys.py:139-144` maps any `IntegrityError`
  to 409 `PASSKEY_ALREADY_REGISTERED`. Probe: 8-byte credential ID (CHECK 16..1023) -> 409 "already
  added". Validate lengths (credential ID 16..1023, public key <= 2048) before insert and return 422;
  keep 409 only for the unique violation (check `error.orig.diag.constraint_name`).
- **L3 Migration lock claim is wrong**: `000016_passkeys.py:21-22,83-86`. Alembic runs one
  transaction (`alembic/env.py:42,61`); `DROP/ADD CONSTRAINT` take ACCESS EXCLUSIVE on `iam.sessions`
  held through `VALIDATE` and commit, so the NOT VALID split buys nothing, and every request
  (session re-check) blocks for the duration. Table is small (30-day purge), so fine operationally;
  correct the header and consider `SET LOCAL lock_timeout`.
- **L4 Defense in depth on `iam.passkeys`**: API gets DELETE and full-column UPDATE under
  `USING (true)` (`000016:73-79`), a step beyond `user_identities` (no API DELETE). Ownership is
  enforced only in Python (`_own_passkey`). Suggest: INSERT/DELETE policies `user_id = iam.actor_id()`
  (registration and removal always run as the owner; forget is DEFINER), and column grants
  `UPDATE (sign_count, backed_up, last_used_at, label)` so no code path can rewrite
  `user_id`/`public_key`/`credential_id`.
- **L5 Config fail-closed is shallow**: `config.py:304-311` rejects only `""`/`"localhost"` and
  non-https prefixes. Passes: `127.0.0.1`, `LOCALHOST`, rp_id with scheme (`https://app.x` -> every
  rpIdHash mismatches, silently broken), `https://localhost` origins, origins on hosts unrelated to
  the RP ID, empty origins list. Normalize/validate: rp_id a lowercase hostname without scheme/port,
  not an IP/localhost; each https origin's host equals rp_id or ends with `.` + rp_id; non-empty list.
  Staging `env.example` omits the Android origin (runbook covers it).
- **L6 Counter regression only refuses**: lib rejects `new <= stored` (and a stored >0 counter that
  later reports 0 locks the credential out). No audit/flag of a suspected clone. Consider an audit
  event on counter regression.
- **L7 `crossOrigin` / `topOrigin` unchecked** (py_webauthn ignores them). If the web client is never
  framed, reject `clientData.crossOrigin == true`.
- **L8 Tests missing**: authenticate-error replay (H1); malformed inputs (M1/M2); userHandle mismatch
  (probe: 401, correct); `id != rawId`; RP ID mismatch (the soft authenticator has `rp_id` but no test
  uses it); disabled/deleted account sign-in; purge of expired `webauthn_challenges` (purge test runs
  the DELETE but asserts nothing about passkey challenges); step-up with zero passkeys.
- **L9 Label**: `PasskeyLabel` lacks the pre-cap `StringConstraints(max_length=400)` that `Name`/
  `Title` use before `clean_text` (whitespace split on up to 1 MiB). NUL is rejected (422, probed).

## Checklist answers
- (a) Challenge: digest via HMAC + `compare_digest`; single-use only when the response verifies and
  `authenticate` succeeds or `verify` returns None; NOT on authenticate errors (H1); also not
  consumed when clientData is malformed or the digest mismatches (harmless). Expiry 5 min checked;
  purpose filtered; step-up challenges bound to the registered actor (probe: other user's challenge
  -> 401). Origin list + RP ID hash + UV enforced by lib. userHandle checked when present; lookup by
  rawId and lib enforces `id == b64url(rawId)`; stored credential ID comes from authData. Backup
  flags stored/updated (BE not stored; fine). `excludeCredentials` set; attestation `none`
  requested (other formats still verified by lib if sent; harmless). Algorithms: EdDSA, ES256, RS256
  (lib default). Zero counters accepted indefinitely (correct for synced passkeys).
- (b) Passkey of A cannot authenticate as B: subject is the row's `user_id`, signature checked
  against that row's key. No account creation (`find_user_id` never returns None for PASSKEY) and no
  link path (`_reauthenticate`). Guest claim reuses `transfer_guest_participations` consent; replay
  hole in H1. Disabled/deleted -> 401 via `_load_active_user`; deletion removes passkeys (tested).
  Registration: fresh step-up + non-guest, checked on both calls; removal step-up; rename no step-up
  (user decision); IDOR: 404 for other users (tested + sweep).
- (c) RLS `USING (true)` matches credential-table convention; `forget_actor_credentials` keeps the
  identity + email-challenge deletes and adds passkeys + challenges; auth_method CHECK widened
  correctly (L3 on lock note); header has all required sections.
- (d) Body capped at 1 MiB globally (`api_max_request_bytes`); see M3 for limits/growth; purge wired.
- (e) See L5. Worker role exempt (correct; it issues no challenges).
- (f) Unknown credential, bad signature, bad UV, bad origin, expired, wrong binding, inactive account
  all -> 401 `authentication_failed`. Timing differs (no signature work for unknown IDs) but
  credential IDs are random >=16 bytes, so not exploitable. 403/409 only after a valid signature.
  Sentry captures no request bodies.
- (g) Soft authenticator is realistic (real P-256 keys, CBOR COSE, `none` attestation, correct flag
  bits and rpIdHash, signature over authData||SHA256(clientData)). One vacuous block (L1), gaps (L8).

## Recommended actions
1. H1: savepoint around `authenticate`/register insert, commit consumption + counter, re-raise;
   bind guest challenges; add the replay test. Decide the merge-consent UX trade-off.
2. M1/M2: catch `WebAuthnException`, validate key loadability and `transports` type; add tests.
3. M3: split rate-limit buckets; consider /64 aggregation.
4. L1, L2, L5 next; L3/L4/L6/L7 at discretion.

## Unresolved questions
- Merge-consent UX after H1: is a second biometric prompt acceptable for guests whose account
  already participates, or should the app ask for merge consent before the ceremony?
- Is the web client ever embedded cross-origin (decides L7)?

Status: DONE_WITH_CONCERNS
Summary: Crypto verification is correct and passkeys cannot cross accounts or create/link accounts,
but a challenge and counter roll back when `authenticate` raises, so a guest's 409 assertion was
replayed signed-out into a full session (proven live); several malformed inputs 500.
Concerns/Blockers: H1 needs a product call on the merge-consent retry UX.
