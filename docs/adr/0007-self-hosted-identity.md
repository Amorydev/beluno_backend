# ADR 0007: Self-Hosted Identity and Infrastructure

**Status:** Accepted (2026-10-06). Supersedes the Supabase Auth/Storage
assumptions in ADR 0001, ADR 0005, and Phases 1–2 of the backend plan.

## Decision

Beluno runs without Supabase. The API is its own identity provider on standard
PostgreSQL; object storage will be an S3-compatible service chosen with the
media phase.

- **Sign-in methods:** Google and Apple ID tokens verified server-side (JWKS,
  issuer, audience = our client IDs, expiry, optional nonce); passwordless email
  with a 6-digit code plus magic link; guest sessions minted by redeeming a plan
  invite that allows guests. No passwords are stored.
- **Tokens:** 15-minute ES256 access JWTs (`iss`, `aud`, `sub`, `sid`,
  `auth_time`, `guest`) signed with keys from `BELUNO_AUTH_SIGNING_KEYS`; the
  first key signs, all listed keys verify, and `/.well-known/jwks.json` publishes
  them. Refresh tokens are opaque, single-use, and stored only as HMAC digests.
- **Account linking:** a Google/Apple identity is never attached to an existing
  account just because its verified email matches (provider email ownership can
  lapse). Sign-in returns `409 ACCOUNT_LINK_REQUIRED`; the account holder signs
  in with an existing method and repeats the Google/Apple sign-in with their
  bearer token within the step-up window to link it.
- **Sessions:** one session per device sign-in with a 90-day idle and 180-day
  absolute lifetime (covering the 90-day offline target). Every request
  re-checks the session and account in PostgreSQL, so logout, remote device
  revocation, and account disabling take effect immediately.
- **Refresh rotation:** presenting a consumed refresh token revokes the session
  (theft signal), except within a 30-second grace window that lets a client
  retry after losing a refresh response; the retry retires the unseen
  replacement so only one live token remains. Presenting that retired
  replacement later also revokes the session, so clients must refresh
  single-flight (one refresh at a time per session, e.g. a shared lock across
  tabs).
- **Step-up:** ownership transfer and deletion require `authenticated_at`
  within 10 minutes; re-authenticating with the same identity refreshes it on
  the current session.
- **Guests:** a guest is a real user row (`kind = guest`). Signing in from a
  guest session either upgrades the guest in place (same user ID, participant
  rows become `identity_kind = user`) or, when the identity already has an
  account, relinks the guest's participant rows to that account and merges
  duplicates only after explicit consent. Participant IDs never change.
- **Email delivery:** the API records a pending challenge and enqueues a
  Procrastinate job in the same transaction; the worker generates the code and
  link token, stores their digests, then sends via SMTP. No secret is persisted
  or queued in plaintext. `console` delivery is development-only.
- **Secrets:** signing keys, `BELUNO_TOKEN_HASH_KEY` (HMAC pepper for refresh
  tokens, invite tokens, email codes, and rate-limit buckets), and SMTP
  credentials come from the secret manager; secure environments fail closed
  without them.
- **Abuse limits:** PostgreSQL fixed-window counters keyed by HMAC of the
  client address or account (no Redis), committed independently so failed
  attempts always count.

## Consequences

- The team owns credential-adjacent code: key rotation runbooks, provider
  client-ID configuration, SMTP reputation, and security review of these flows
  are launch gates.
- MFA and passkeys are not part of this release; the session and step-up model
  leaves room to add them.
- Phase 7 (lifecycle/integrations) chooses the production SMTP relay and object
  store; Phase 8 adds key-rotation and abuse drills to release evidence.
