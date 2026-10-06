# 2026-10-06 — Phase 3: identity, groups, plans

## What changed

- Dropped Supabase mid-phase (ADR 0007). The API now owns identity: Google/Apple ID tokens, passwordless email (code + magic link via a Procrastinate job), invite-minted guests, ES256 access JWTs with JWKS, rotating HMAC-stored refresh tokens, per-request session re-validation, step-up via `authenticated_at`.
- Built groups (memberships, invite links, atomic owner transfer, deletion grace), generic plans (timing, lifecycle, stable participants, RSVP independent of access, travel extension), plan invites + placeholder/guest claims with explicit merge consent, recurring series (RRULE subset, DST-safe, idempotent occurrences, this-and-future split) and allowlisted duplication.
- Central pure policy + executable permission matrix (docs table parsed by tests); RLS on every table via SECURITY DEFINER helpers; migration `000003` adds BEFORE-trigger write guards against insiders.
- Every mutation writes `audit_events` + `change_log` in the same transaction (Phase 4 extends this seam).

## Lessons

- RLS shapes ORM usage: `INSERT ... RETURNING` and `ON CONFLICT` both evaluate SELECT policies against rows the actor cannot see yet; `SELECT ... FOR UPDATE` applies UPDATE policies (turned 403 into 404 until reads fell back to unlocked).
- RLS is not enough against insiders: the reviewer moved rows across tenants and self-reactivated; triggers fixed it, with an "owner seat vacant" exception so two-step ownership transfer still works.
- Phase 2 had never touched a real database (Docker absent → skipped): migrator lacked CREATE, Procrastinate reused a closed pool, coverage under-reported without greenlet tracing. A local PG18 + `BELUNO_TEST_ADMIN_DATABASE_URL` made the suite real.
- Subagent reports need re-verification: the tester claimed lint-clean and shipped a vacuous rate-limit test.

## Decisions taken with the user

Self-hosted stack; nullable plan group; explicit account linking instead of email auto-link; keep session revocation on retired refresh replacement (single-flight refresh); placeholders member/viewer only, guest claimers get the guest role.

## Open

Phase 4 idempotency/sync; Phase 7 notifications and purge jobs; proxy `--forwarded-allow-ips`; MFA/passkeys; staging and the unverified Phase 2 rehearsals.
