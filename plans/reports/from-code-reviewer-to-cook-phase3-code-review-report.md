# Phase 3 code review: identity, groups, plans, invites, series, RLS

Date 2026-10-06. Read-only review; no project files modified. Probes ran from a scratchpad pytest file against the live PG 18 on :54330 (fixtures loaded via `-p conftest`).

## Verdict

- **Score: 7/10**
- **Decision: PASS_WITH_RISK.** No API-level cross-tenant IDOR found. The 404/403 split, step-up, invite hashing, last-use locking, owner invariants and current-state session checks all hold under test. 11 issues are proven by probes (4 high). Fix W1–W6 before production traffic.

## Verification run

| Check | Result |
|---|---|
| `uv run ruff check .` (excluding the tester's in-flight `tests/integration/test_phase3_edge_cases.py`, created 13:36 during this review) | All checks passed |
| `uv run ruff format --check .` (same exclusion) | 134 files already formatted |
| `uv run mypy src` | Success, 85 files |
| `pytest -q --ignore=tests/integration/test_phase3_edge_cases.py` | **266 passed** |
| Full `pytest -q` including the in-flight tester file | 282 passed, 1 failed (`test_admin_cannot_promote_to_admin`), 2 skipped. That file also has 3 ruff errors plus 1 format error. It is not part of the cook implementation, but it will break CI if it is committed as-is. |

## Critical (must fix)

None proven. No unauthenticated or cross-tenant exploit path through the API was found.

## Warnings (substantiated, ordered by risk)

**W1. A client-controlled `X-Request-ID` longer than 128 chars returns 500 on every mutation, including sign-in. (High, proven)**
- Location: `src/beluno/api/main.py:44` stores the raw header. The value goes into `sync_audit.audit_events.request_id CHECK (char_length <= 128)` (migration `:374`) and `change_log` (`:397`).
- Probe: `POST /v1/plans` with a 200-char `X-Request-ID` raises `CheckViolation audit_events_request_id_check`, so the response is 500.
- Impact: any proxy or SDK that chains or sends long IDs breaks all writes. The unvalidated value is also written verbatim into audit rows (log forging).
- Fix: in the middleware, accept only `^[A-Za-z0-9._-]{1,128}$` and otherwise generate a UUID.

**W2. A removed group invitee can still become an active member (lost update). (High, proven)**
- Location: `modules/groups/service.py:257-273`. `answer_invitation` loads the group without a lock (`:258`). For invited users the RLS update policy would hide the row anyway, so `for_update` would not help. The membership is read without `FOR UPDATE`, and the ORM `UPDATE` matches on the primary key only.
- Probe: held the membership row lock, sent accept, then committed `state='removed'`. Result: accept returned 200 and the final state is `active`.
- Violates: "Removed users lose access immediately".
- Fix: re-read the membership with `session.get(..., with_for_update=True)` and check `state == 'invited'` after the lock. Alternatively add `version` to the WHERE clause (version_id_col).

**W3. RLS UPDATE policies let insiders move rows across tenants or re-activate themselves. (High for defense in depth, proven as api_runtime with `app.actor_id`)**
- `plan_participants_update` (migration `:719-728`): a removed participant can run `UPDATE ... SET access_state='active', role='admin'` on their own row (1 row updated), and the plan becomes visible again. The WITH CHECK only requires "any row in the plan".
- `group_memberships_update` (`:647-649`): the actor can move their own membership into a foreign group as admin (1 row). The WITH CHECK allows `user_id = actor` for any `group_id`.
- `plans_update` (`:698-700`): any active participant, including a viewer, can set `group_id` to a group they do not belong to and `visibility='group'` (1 row). This pushes plan data to that group.
- Same pattern elsewhere:
  - `group_memberships_insert` (`:640-646`) does not restrict `role`.
  - `plan_invites_update` / `group_invites_update` (`:737-745`, `:658-666`) let a token holder rewrite `role`, `max_uses` and `expires_at`.
- Effect on acceptance: "RLS and API policy both deny cross-group/plan access" is only **partially met**. The existing tests cover outsiders only.
- Fix (any of):
  - Column-level `GRANT UPDATE (...)` that excludes `plan_id`, `group_id`, `user_id`, `token_hash` and `role`.
  - A BEFORE UPDATE trigger that rejects changes to tenant keys.
  - Tighter WITH CHECKs: `plans_update` must re-check group membership; a self-update must not raise `access_state` or `role`.

**W4. Google/Apple identities auto-link to an existing account by verified email. (High, security decision needed, proven)**
- Location: `modules/iam/sign_in.py:61-72`.
- Probe: a user signs in with email `victim@corp.example`. A different Google `sub` with the same verified email then lands in the same account (`AUTOLINK_SAME_USER True`). This is also asserted as intended in `test_magic_link_signs_in_once_and_links_google_by_verified_email`.
- Risk: Google is authoritative for `email_verified` only for gmail.com or when an `hd` claim is present. For other domains, Apple and Google verified ownership only once (former employee, lapsed domain), so this is an account-takeover path.
- Not covered by ADR 0007. Options:
  - (a) auto-link only for gmail.com / `hd` / Apple private-relay addresses;
  - (b) require an email OTP before linking;
  - (c) keep the current behavior and accept the risk in the ADR.

**W5. Credentials leak to Sentry on 5xx. (Medium-high, proven)**
- Location: `observability/redaction.py:8-32` lacks `id_token`, `link_token`, `code`, `nonce` and `intended_email`.
- Sentry 2.71 `StarletteRequestExtractor` attaches JSON bodies regardless of `send_default_pii=False`. Its default `EventScrubber` also left those keys intact (probe output).
- Scenario: a 500 inside `/v1/auth/email/verify` rolls back the challenge consume, so the plaintext magic-link token or OTP sitting in Sentry stays usable for 10 minutes.
- Fix: add those keys, or redact `request.data` wholesale for `/v1/auth/*` and `/v1/invites/*`.

**W6. Per-client rate limits become global behind a proxy. (Medium-high, deployment)**
- Location: `api/dependencies.py:51-54` keys on `request.client.host`. Uvicorn trusts `X-Forwarded-For` only from `127.0.0.1` by default.
- Effect: behind a load balancer every user shares one bucket. 20 email challenges/hour and 10 guest creations/hour become platform-wide, so one actor can lock out sign-in.
- Fix: document and enforce `--forwarded-allow-ips`, or derive the subject from a trusted header.

**W7. Invite redemption ignores plan state. (Medium, proven)**
- Location: `modules/plans/invites.py:234-236` checks only `deletion_scheduled_at`.
- Probe: registered users and new guests join a **cancelled** plan via an old link (200, active).
- Conflicts with the matrix: completed, archived and cancelled plans are read-only, and `plan.join` requires editable states.
- Fix: in `redeem`, raise `invite_unavailable()` unless `plan.state` is in `EDITABLE_PLAN_STATES`.

**W8. A claim link for a removed placeholder still binds the redeemer. (Medium, proven)**
- Location: `_claim_placeholder` (`plans/invites.py:310`) checks `identity_kind` but not `access_state`. Removing a placeholder does not revoke its claim invites.
- Probe: claim after removal returns 200 and links the user to a `removed` row (the invite use is consumed). That user is now locked out of normal joins ("You were removed").
- The router maps every non-active state to `"pending_approval"` (`api/routers/invites.py:283-287`), so the response is mislabeled.
- Fix: require `access_state == 'active'`, revoke claim invites on placeholder removal or merge, and map states explicitly.

**W9. Guest identities can hold member/admin-level placeholder roles, bypassing the matrix. (Medium, proven)**
- Placeholders can be seeded as `admin` (`contracts/plans.py:25,73`). Yet `change_role` forbids admin for non-user identities (`participants.py:209`), which is inconsistent.
- A guest who claims a placeholder is only downgraded admin→member (`plans/invites.py:329-330`). The probe shows the guest keeps `member` and gets 200 on `PUT /travel`. The matrix says only guest-role identities may be guests and that they cannot manage travel.
- A registered claimer of an admin placeholder becomes plan admin via a bearer link.
- Fix: guest claimers take `guest` (or `viewer` if the placeholder was viewer). Disallow `admin` on placeholder seeds, or document it explicitly.

**W10. A timezone-aware `local_start_time` returns 500. (Medium, proven)**
- Location: `contracts/plans.py:297,311` accept `time` with an offset. Then `series.py:334-340` calls `datetime.combine` and `timing.py:31-32` raises `ValueError("local time must be naive")`, which is unhandled (probe: `ValueError`).
- When no occurrence falls in the horizon, the request returns 201 and the offset is silently dropped.
- Fix: reject `tzinfo` in a validator on both request models.

**W11. A stale refresh token presented within the grace window revokes the whole session. (Medium, availability; touches a user decision)**
- Location: `sessions.py:263-282`. A replacement retired by a grace retry has `replaced_by_hash IS NULL`, so presenting it fails the grace check and revokes the session (`:132-138`).
- Probe: refresh T twice, then present the first replacement. Result: 401, the second replacement also 401, and `/v1/me` 401.
- Any client with concurrent refreshes (two tabs, retry racing the original) gets logged out. This matches ADR "reuse revokes" literally, so it is a trade-off and not a defect. Options:
  - (a) treat a retired-within-grace token as a plain 401 without revoking;
  - (b) require clients to serialize refreshes and document it;
  - (c) keep as is.

## Suggestions (lower risk)

- **S1. Rotation can renew dead invites.** Rotating an exhausted, revoked or expired invite issues a fresh one with `max(1, 0) = 1` use and at least 1h (`plans/invites.py:163-179`, probe `ROTATE_EXHAUSTED 201 1`). Reject rotation of unusable invites, or document that it re-arms them.
- **S2. Removal is bypassable through guest links.** `allow_guests` defaults to `True` (`contracts/invites.py:23`), so a removed person can rejoin under a new guest identity through any live link. Consider auto-rotating or prompting on removal, or defaulting `allow_guests` to false.
- **S3. Invites outlive their creator's rights.** Invites created by a manager stay valid after that manager is demoted or removed, including owner-only admin invites after an ownership transfer. Consider re-checking the creator's current rights at redemption.
- **S4. Series materialization is slow inside the request.** It runs synchronously in `POST /v1/plan-series`: up to 60 occurrences × (1 + ≤100 attendees), each with a flush and 2 audit inserts, so roughly 18k round trips in one transaction (`series.py:251-292, 385-423`). Batch inserts, or cap the API path to a few occurrences and leave the rest to the worker.
- **S5. Several races return 500 instead of 409.** Concurrent `join`, concurrent participant add, and a concurrent first sign-in for a provider identity without email all hit unhandled `IntegrityError`. Map unique violations to 409.
- **S6. `/internal/whoami` skips the session check.** It does not go through `open_context` (`api/routers/internal.py:39`), so a revoked session's token keeps working there until it expires. It is a Phase 2 diagnostic route; route it through `load_current_actor` or remove it.
- **S7. New functions will default to PUBLIC EXECUTE.** No `ALTER DEFAULT PRIVILEGES ... REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` exists for the `iam`, `groups` and `plans` schemas (`sql/platform-runtime-grants.sql:20-21`), so future SECURITY DEFINER helpers would be PUBLIC-executable.
- **S8. Stale Supabase docs.** `README.md:7` and `docs/architecture/system-context.md:7-9` still describe Supabase Auth/Storage. The OpenAPI document has no examples (plan step 17).
- **S9. Plan-level rate limits are missing.** There are no limits on plan participant add/remove/role or on ownership transfer (plan step 17 lists membership changes and ownership transfer).
- **S10. Large modules.** `series.py` (501 lines), `groups/service.py` (437), `plans/invites.py` (429), `routers/plans.py` (409) and `participants.py` (376) all exceed the 200-LOC guidance. Split only where it reduces complexity.

## (a) Acceptance criteria

| Item | Status | Evidence |
|---|---|---|
| Auth subjects map idempotently to users/devices | Met (edge race S5) | `test_google_sign_in_creates_one_account_per_subject` |
| Group lifecycle incl. owner transfer/delete | Partially | Works and tested; W2 accept/remove lost update |
| Generic plan schema (casual + multi-day) | Met | `test_movie_plan_needs_no_travel_fields`, migration CHECKs |
| RSVP independent, audited, delta-visible | Met | `test_rsvp_is_independent_from_access` |
| Series deterministic, TZ-correct, exceptions | Mostly | Unit DST tests + idempotent worker; W10 |
| Duplication allowlist only | Met | `plan-copy-v1`, `test_duplicate_copies_only_the_allowlisted_manifest` |
| Participant IDs stable through leave/remove/claim/merge | Met | guest-claim and e2e tests |
| Travel optional | Met | |
| Invites hashed/expiring/revocable/limited/rate-limited | Mostly | W7, W8, S1 |
| Guest claim atomic/audited/duplicate-safe | Met for atomicity; role rule broken | W9 |
| Permission matrix covers all cells | Met for the pure policy | Service paths bypass it (W7, W9) |
| RLS and API both deny cross-tenant access | **Partially** | W3 |
| OpenAPI + delta events for all mutations | Mostly | No examples (S8) |
| Test matrix: Critical rows | Met | IDOR, last-use concurrency, merge, last owner, removed JWT all tested |
| Test matrix: High/Medium rows | Met | Finance and signed-URL aspects deferred to later phases |
| Success criterion: central check on every endpoint | Partially | Series uses RLS + ad-hoc creator check; `whoami` (S6) |
| Success criterion: removed users lose access immediately | Partially | W2, W3 |
| Success criterion: deterministic time in suites | Partially | Integration uses the wall clock (`create_app(clock=)` exists but is unused) |

## (b) Phase 2 regression check

- `health`, `/internal/whoami` 401/503 contract, request body limit, problem+json handlers and OpenAPI snapshot: covered by passing contract tests.
- Config aliases (`database_url`, `assert_production_requirements`) and the `Database.session()` compatibility helper are kept.
- Bootstrap and grants order is verified by `test_platform_bootstrap` and the RLS ownership test.
- The worker now also serves `email` and `plans` queues and runs periodic purge and series jobs. Procrastinate dedupes periodic defers. The `api_runtime` grants on `jobs` are insert-only plus the defer function.
- Side effects that would show up as regressions:
  - W1: proxies or SDKs that send long request IDs.
  - W6: per-IP limits behind a load balancer.
  - Old Supabase env vars are now silently ignored (`extra="ignore"`). Secure environments still fail closed without the new keys.

## (c) Public contracts

Intended breaks: the Supabase env vars were removed and `JwtVerifier` was replaced by `AccessTokenCodec`. No other removed or renamed public symbol or env var was found.

New, unflagged behavior changes:
- `X-Request-ID` is now effectively length-limited and returns 500 above 128 chars (W1).
- `/internal/whoami` semantics are unchanged but it is not session-checked (S6).

I could not diff OpenAPI against the Phase 2 base because the repo has no git history; the `check_openapi_compatibility.py` CI step covers this on PRs.

## (d) Patterns

Consistent throughout:
- `open_context` → `load_*` → `require_*` → mutate → `record_mutation`.
- No RETURNING or ON CONFLICT on RLS-hidden inserts.
- `If-Match` on versioned roots.
- Uniform `invite_unavailable`.
- External I/O (JWKS, SMTP) stays outside DB transactions.
- Rate limits commit in their own short transaction before `open_context`, so there is no nested pool-connection deadlock.

Minor duplication: `MANAGE_ERRORS` equals `PUBLIC_ERRORS`, and two module-level `security_log` objects exist.

## (e) Lint/type/build

Clean for the implementation (see the verification table). The tester's in-flight file is not.

## disprovenClaims

- "RLS repeats tenant/current-membership constraints as defense in depth" (migration docstring and permission matrix): disproven for insiders (W3).
- "Rows stay inside plans where the actor already has a row or holds an invite" (migration `:717-718`) is technically true, but it allows self-reactivation as admin (W3).
- "Removed/left users lose reads … immediately": disproven under the accept/remove race (W2).
- "guest role belongs to guest identities" (permission matrix): disproven via placeholder claim (W9).
- "completed/archived/cancelled plans are read-only except state/duplicate/delete": disproven for invite joins (W7).
- "Metadata is redacted … no caller can leak tokens" holds for audit rows. It does not hold for Sentry request bodies (W5).

## unverifiedClaims

- Invite preview/redeem is "timing-safe". An unknown token does 2 lookups and an expired one does 1. This is not exploitable given 256-bit tokens, but it was not measured.
- Uvicorn proxy configuration in production (W6): deployment is not in the repo.
- Contract compatibility with the Phase 2 OpenAPI baseline (no git base).

## missingProof

- No RLS tests for insiders: removed participant, viewer, invite-holder UPDATE paths (W3).
- No test for concurrent accept vs remove of a group invitation (W2) or for concurrent plan ownership transfer (only group transfer is tested).
- No test that removing a placeholder invalidates its claim link (W8), that redeem is blocked on closed plans (W7), or for guest-claim role limits (W9).
- No test for an oversized or malformed `X-Request-ID` (W1), an aware `local_start_time` (W10), or Sentry body redaction of auth fields (W5).
- Integration series/DST tests use the wall clock rather than the injectable `clock`.

## Unresolved questions

1. Is auto-linking a Google/Apple identity to an existing account by verified email an accepted product risk (W4)? The ADR is silent.
2. Should the refresh grace path revoke on a retired replacement (W11)? This needs a decision that keeps the 30 s grace intact.
3. Should placeholders ever carry `admin`, and should guest claimers keep member rights (W9)?

Status: DONE_WITH_CONCERNS
Summary: Phase 3 is architecturally sound and passes ruff, mypy and 266 tests, with no API-level IDOR found. Probes proved 11 issues; 4 are high (request-ID 500, accept/remove lost update, RLS insider row-move/self-reactivation, email auto-link takeover path).
Concerns/Blockers: Fix W1–W6 before production exposure; W4 and W11 need user/security decisions. The tester's in-flight `test_phase3_edge_cases.py` currently fails lint and has 1 failing test.
