# Code review: trip planning bookings (secrets sealed at rest)

Branch `feat/planning-bookings`, uncommitted tree, reviewed 2026-10-07. Read-only. Every finding below
marked "probe-confirmed" was reproduced against the live PG18 cluster with a throwaway pytest file in
the session scratchpad. That file is not in the repo.

## Scope
- Files: secret_box.py, config.py, 000013_bookings.py, models/bookings.py, planning/{bookings,costs,itinerary}.py,
  contracts/planning.py, api/{commands,routers}/planning.py, planning_projection.py, projection.py, rate_limits.py,
  generate_signing_key.py, deploy/staging, testkit, tests, docs. About 1.3k new LOC plus the diff.
- Gates I ran:
  - `ruff check`: clean.
  - `ruff format --check`: clean.
  - `mypy src` (strict): clean.
  - pytest over bookings, secret_box, config, tests/security, plan_purge, places_itinerary, and telemetry_redaction: **258 passed, 0 skipped**.
  - OpenAPI snapshot matches `create_app().openapi()`.
  - Compatibility check against HEAD: exit 0. The changes are additive: 5 new schemas, and `booking_id` on the item request and response.

## Overall assessment
The core crypto is sound:
- AES-256-GCM with a fresh 96-bit nonce per seal;
- AAD binds the booking id and the field;
- the key id is stored per row;
- the keyring is validated at config load.

The masking in responses, sync, the change feed, conflict `current`, and activity events is correct.

Two side channels defeat "sealed at rest / never logged" for realistic inputs: the unsalted idempotency
request hash, and Sentry frame locals. Several lifecycle edges are confirmed live: merged or left travelers,
deleted bookings, and cancelled-then-revived paid costs.

## Critical
None.

## High

### H1. The idempotency `request_hash` is an offline oracle for confirmation codes (probe-confirmed)
`src/beluno/sync/idempotency.py:38-49`. The hash is stored in `sync_audit.operations.request_hash` for 180 days, outlives the plan purge, and is never keyed.
- **What leaks:** an unsalted SHA-256 over canonical JSON that includes `payload.secrets.confirmation_code` and `private_notes` in plaintext.
- **What the attacker needs:** the at-rest threat model is a DB dump, backup, or read role. Everything else in the canonical form is readable from plaintext rows: command name, `plan_id`/`booking_id`, `expected_version`, kind, title, dates, travelers, and price. The set of fields the client sent (`exclude_unset`) is a small enumeration.
- **Cost:** a 6-character PNR is about 2^31 candidates, seconds on a GPU.
- **How common:** every sync push carries an `operation_id` that doubles as the idempotency key, so offline clients produce one of these hashes on almost every booking write.
- **Probe:** a REST create with `Idempotency-Key` stored the hash, and brute force over 36 candidates recovered `Q7XK` from the stored hash.
- **Fix:** make the digest keyed. Either:
  - HMAC the whole canonical form with a server key (`token_hash_key` already exists). To avoid 409 `IDEMPOTENCY_KEY_REUSED` on in-flight retries across the deploy, compare against the legacy digest for one retention window.
  - Or, narrower: add `Command.secret_fields`, and replace those values with `HMAC(key, value)` before hashing.
- **Test to add:** the stored hash differs from the plain SHA-256 of the canonical JSON.

### H2. Sentry frame locals carry booking plaintext (probe-confirmed with the repo's own `redact_sentry_event`)
`src/beluno/observability/setup.py:30-38`.
- **Cause:** sentry-sdk 2.71 defaults to `include_local_variables=True`. `redact()` only matches mapping keys from `SENSITIVE_KEYS`. Local variables such as `draft`, `body`, `secrets`, and `plaintext` hold reprs that contain the code and notes. (`secrets` is not in the list; `secret` is.)
- **Trigger:** any unhandled exception in the booking write or reveal path, with a Sentry DSN set. Real 500s exist: L1 below, DB errors, `SecretBoxError` on reveal.
- **Probe:** `capture_exception` from a frame holding `BookingDraft(secrets=Secrets("PNR-ZX81QK", "door 4471"))` exported both strings.
- **Scope:** the same exposure applies to OTP codes and tokens elsewhere, so this predates the slice. It contradicts the slice's explicit "never logged" promise.
- **Fix:** pass `include_local_variables=False` to `sentry_sdk.init`, or drop `exception.values[].stacktrace.frames[].vars` in `redact_sentry_event`.
- **Test to add:** a unit test around `redact_sentry_event` with a captured frame.

## Medium

### M1. Merged or departed travelers lose reveal, and the booking can no longer be saved unchanged (probe-confirmed)
`alembic/versions/000013_bookings.py:116-119` and `src/beluno/modules/planning/bookings.py:270-284`.
- **Cause:** `actor_may_reveal` matches only `p.id = ANY(traveler_ids)` against the actor's own active row. A placeholder merged into an existing member (claim with `merge_existing`) leaves `traveler_ids` pointing at the merged row. A guest merged into an account does the same.
- **Probe:** booking for Cam (placeholder); Cam's claim merged into Bea. Bea's reveal returned **403**. The owner's PUT with unchanged `traveler_ids` returned **422** "traveler_ids must name active participants". The same 422 follows for any traveler who left or was removed.
- **Fix:**
  - In the definer, also accept `t.merged_into_participant_id = p.id` for traveler rows.
  - In `_apply`, require active only for travelers newly added versus `booking.traveler_ids`. Alternatively, remap merged ids at merge time, as finance does for balances.

### M2. Sending `secrets` with one field silently clears the other (probe-confirmed)
`src/beluno/contracts/planning.py` (`BookingSecretsRequest`, defaults `None`) and `bookings.py:305-307`.
- **Behaviour:** `{"secrets": {"confirmation_code": "A2"}}` wipes the private notes (`has_private_notes` became `False`). Nothing can recover them, because they are encrypted and not versioned.
- **Why it bites:** clients show masked values. A client that edits only the code must first reveal the notes (an audited, rate-limited call) just to avoid losing them.
- **Fix:** treat omitted as "keep" and explicit `null` as "clear", using `model_fields_set`. Carry a sentinel through `Secrets`, and in `_seal` re-seal only the provided fields. Mixed key ids then need a key id per field, or re-sealing kept fields means opening them.
- **Alternative:** require both keys to be present (no defaults) so the footgun is explicit.

### M3. Deleting a booking leaves dangling references and secrets kept forever (probe-confirmed for items)
`bookings.py:204-213`.
1. Itinerary items keep `booking_id` pointing at the tombstone. Re-saving the item unchanged returns **422** "booking_id does not name a booking of this plan" (`itinerary.py:353-360`). The same applies to a booking pointing at a deleted place: `bookings.py:260-269` (places already behaved this way for items).
   - **Fix:** in validation, accept an id equal to the stored value, or unlink items on delete. Unlinking bumps item versions, so it needs change_log rows.
2. The sealed `booking_secrets` row survives the tombstone until plan purge, possibly forever. `actor_may_reveal` excludes deleted bookings, so nobody can ever use it.
   - **Retention:** the retention matrix says "cleared when set to null" but is silent on delete.
   - **Rotation:** the rows pin old keys forever. The runbook's "old keys stay until no row uses them" can never be met, because only a write that includes secrets re-seals a row.
   - **Fix:** null both blobs (or the whole row's content) in `delete_booking` *before* setting `deleted_at`, because the UPDATE policy needs `deleted_at IS NULL`. Make the runbook query `JOIN bookings` and filter out tombstones, or add a re-seal job.

### M4. A paid booking that is cancelled and then revived loses its cost when the expense is voided (probe-confirmed)
`src/beluno/modules/finance/commitments.py:141-199` and `planning/costs.py:83-95`.
- **Sequence:** confirmed with price → expense paid it (converted) → cancelled. `cancel()` sets `converted_from_state='cancelled'`. Then re-confirmed with the same price. `sync_cost` treats the converted row as unchanged and returns early. If the price changed, `record()` keeps `converted_from_state` anyway.
- **Probe:** after voiding the expense, the booking is `confirmed` with a price but `commitment_id=None`. The row is `('cancelled', None)`, so the cost is gone from budgets until the next priced edit happens to revive it.
- **Related tier drift:** planned → paid → confirmed → void restores `estimated` while the booking is confirmed.
- **Scope:** both are latent port semantics from slice 1. Bookings make them routine, because cancel and re-confirm is normal.
- **Fix:** when `record()` touches a converted or refunded row, set `converted_from_state = draft.state`, which clears the withdrawal marker and keeps the tier in step. `sync_cost` must not short-circuit while `converted_from_state` differs from `tier`.

### M5. The secrets RLS is only as strong as API authorization: any active participant can make themselves a traveler (probe-confirmed)
`000013_bookings.py:128-172`.
- **Cause:** the `bookings` UPDATE policy and guard allow every active participant, viewers included, to change `traveler_ids` with `version+1`.
- **Probe:** as Dan (a member, not a traveler, not the creator) over the API role, `SELECT count(*) FROM booking_secrets` went 0, then `UPDATE ... traveler_ids || dan` (with `version+1`), then the count was 1.
- **Threat model:** the ciphertext is useless without the API-held keys. So this is defense in depth only, but the header's claim ("read and written only by whoever may reveal them") holds only while API authorization holds.
- **Fix:** in `guard_booking`, when `traveler_ids` (or any field) changes, require the actor to be the creator (or a guest merged into them) or an owner/admin. This mirrors `require_author_or_manager`. Alternatively, reword the header and docs.

## Low

- **L1. Key ids longer than 64 characters pass validation but fail every seal.**
  - `secret_box.py:43-45` accepts any non-empty key id. `booking_secrets.key_id` has `CHECK BETWEEN 1 AND 64`.
  - A long kid passes config validation, then every seal fails with CheckViolation → 500, and the plaintext reaches Sentry frames (H2).
  - Fix: validate `1 <= len(key_id) <= 64` in `SecretBox.__init__`.
- **L2. A config validation failure echoes key material into startup logs (probe-confirmed).**
  - `Settings(booking_keys=...)` raises a pydantic error whose `input_value` shows the truncated tail of the keyring JSON.
  - With an active-id typo, which is likely during rotation, 19 base64 characters (about 14 of 32 bytes) of the last key reach startup logs. The same pattern affects `token_hash_key` and `auth_signing_keys` (pre-existing).
  - Fix: add `hide_input_in_errors=True` to `SettingsConfigDict` (`config.py:52`).
- **L3. Reveal failures are unmapped 500s.** `bookings.py:234-238`. An unknown key id or tampered blob raises `SecretBoxError(ValueError)`. Map it to a stable problem code (e.g. 409 `SECRET_UNREADABLE`) and log only the key id.
- **L4. Audits say nothing about secrets.**
  - Booking-update audits carry `{version, status}` only (`bookings.py:372`). Add `secrets_changed: bool` (never values) for forensics.
  - Denied reveals (403) leave no audit, because the context rolls back; consider a security event.
- **L5. Older clients can silently unlink items.** `ItineraryItemRequest.booking_id` is a new optional field on a full-replace PUT. A client that does not know the field unlinks the item on every save. Acceptable before release; note it in the sync contract.
- **L6. Test hygiene.** In `tests/unit/test_secret_box.py:13`, `keyring(active, ...)` ignores `active` (`del active`), which is dead API.

## Verified clean
- **Crypto:**
  - nonce from `os.urandom(12)` per seal;
  - the tag is verified, and tampering, a wrong owner, or a wrong field all raise;
  - AAD `beluno:{field}:{booking_id}` is unambiguous;
  - the key id is per row, and rotation reads old and writes with the active key;
  - base64 `validate=True`; 32-byte length enforced; the active key must be present;
  - DB length CHECKs (29..1024 and 29..8192) fit the contract maxima after UTF-8 expansion.
  - The `lru_cache` keyed on the keyring string is harmless: the same secret already lives in Settings, and the `AESGCM` repr exposes no key.
- **Missing keys:**
  - a secure API or ALL role refuses to start;
  - a worker or scheduler does not need the keys;
  - compose passes the keys to `api` only;
  - writing a secret without keys returns 503, and a write without secrets still works.
- **Masking:**
  - list, get, the create/update responses (stored `response_body`), sync snapshot and change loaders, conflict `current` (`present_planning_current`), activity summary (`booking_kind` only), and audit metadata carry no plaintext;
  - the 422 handlers (REST and push) never echo values;
  - the telemetry redaction test covers the code in logs and spans.
- **Reveal:**
  - VIEW, then `actor_may_reveal` (definer, `search_path` pinned, EXECUTE revoked from PUBLIC and granted to `api_runtime`; requires active, deleted bookings excluded, roles match `MANAGE_PLANNING`, creator merge via `merged_into_user_id`);
  - the rate limit is committed separately before the context;
  - the audit sits in the same transaction;
  - `Cache-Control: no-store`;
  - the endpoint is not a command.
- **Editors versus revealers:**
  - `require_author_or_manager` (author, merged guest, owner/admin) is a subset of `actor_may_reveal`, so no editor is blocked from re-sealing. The exception is M1-style travelers, who are not editors anyway.
- **Write ordering:**
  - create flushes the booking, then the secrets, then the cost;
  - update bumps the version and flushes before the secrets read and the port;
  - delete calls the port while the booking is still clean, then bumps;
  - no unbumped dirty flush.
- **IDOR:**
  - `place_id` and item `booking_id` use composite `(plan_id, id)` foreign keys plus validation;
  - `traveler_ids` is checked by both the guard and the API;
  - the IDOR sweep includes bookings.
- **The `costs.py` extraction is behaviour-identical for items:**
  - the withdrawal predicate is equivalent under De Morgan;
  - the added tier term is a no-op at the ESTIMATED tier.
- **Kill switch:** applied only when a commitment write happens.
- **Purge and migration:**
  - purge order: items → booking_secrets → bookings → place_reactions → places;
  - no DELETE grant on either table;
  - the purge test counts rows in the `bookings` schema;
  - the migration header has its sections; the FK validation runs on an all-NULL new column.
- **OpenAPI:** the snapshot is fresh and the changes are additive.

## Test gaps
- No integration test for:
  - `booking.delete` (cost withdrawal, the tombstone, item references);
  - a non-author member's PUT or DELETE returning 403;
  - pushing booking commands through `/v1/sync/push`;
  - conflict `current` masking;
  - merged or left travelers;
  - reveal by a guest-merged creator;
  - rotation end to end (a row sealed under k1 still revealed after k2 becomes active).
- After fixing H1 and H2, add regression tests: the stored hash is keyed, and Sentry events carry no frame vars.

## Recommended actions (priority order)
1. H1 keyed request hash; H2 disable Sentry locals.
2. M2 omitted versus null secrets; M1 merged and inactive travelers; M3 delete clears secrets and accepts unchanged dangling references.
3. M4 port: reset `converted_from_state` on revival; M5 guard pins who edits a booking.
4. L1 to L6, plus the test gaps.

## Unresolved questions
1. M2: should omitted mean keep (my recommendation), or should both fields be mandatory?
2. M3: on booking delete, should linked items be unlinked, or keep the tombstoned reference with validation relaxed?
3. H1: is a one-retention-window dual-hash acceptable, or is a hard switch fine because there are no production clients yet?

Status: DONE_WITH_CONCERNS
Summary: Crypto, masking, the reveal path, purge, and write ordering are sound, and all gates pass (258 tests, ruff, mypy, OpenAPI). Two side channels defeat the at-rest and no-logs promise (H1 unsalted idempotency hash, H2 Sentry frame locals), and five lifecycle or defense-in-depth defects were confirmed live (M1–M5).
Concerns/Blockers: H1 and H2 should be fixed before merge. M1 to M4 are user-visible data and permission bugs.
