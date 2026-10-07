# Code Review: paid plans / entitlements (feat/entitlements, uncommitted)

## Scope
- Files: migration 000021, stores.py, modules/billing.py, routers/billing.py, contracts/models, plans/service.py, media.py, context.py, main.py, worker/tasks.py, config.py, staging compose/env, tests, docs (~2.2k LOC new + ~1.1k changed incl. openapi)
- Gates: ruff check/format clean; mypy strict clean; billing + google client tests 22 passed; media, duplication, lifecycle, purge, sync_push, rls_isolation 35 passed (live PG). OpenAPI export current; compat check vs main rc=0, only additive schema/path changes.
- Probes (scratchpad `test_billing_probe.py`, live PG): restore bypass, admin-reopen bypass, sticky Apple revoke, token-less claim, Production w/o app id, purge with a pass attached (OK, plan_id -> NULL).

## Overall
Solid core: store I/O happens outside transactions, definer functions own all writes, RLS limits SELECT to the purchase owner, plan_unlock returns NULL to non-participants, Xcode/LocalTesting cannot be configured (Literal), concurrent creates are serialized by an advisory lock in READ COMMITTED. Acceptance criteria 2 and 3 are **not** met: confirmed limit bypasses, and Apple refunds permanently kill a renewing Pro.

## Critical
None.

## High

**H1. Free trip limit bypassable by restore, non-owner reopen, and ownership transfer (AC2).**
- `src/beluno/modules/plans/service.py:389-399` `restore_plan` clears `deletion_scheduled_at` with no limit check; `trips_counting` excludes scheduled-deletion trips (migration:228). Probe: 2 trips -> DELETE first -> create third -> restore first => `active_trips 3, limit 2`. Repeatable without bound.
- `service.py:358-361` checks reopen only when the actor is the owner. Probe: owner makes a friend admin, completes trip, creates 2 more, admin reopens => 3/2.
- `participants.py:406` transfer_ownership never checks the new owner; docs/contracts/billing.md says so explicitly, but it lets one free account park unlimited trips on a helper account while staying admin (manager rights intact). Implementer choice, not a listed user decision.
- Fix: call `billing.require_room_for_trip` in `restore_plan` (trip, editable state, owner = plan owner), and in change_state check the plan **owner's** count regardless of actor (parameterize `require_room_for_trip` by owner user id; advisory lock + trips_counting must take the owner id, so trips_counting needs a `p_owner` variant restricted to plans the actor is in). For transfer: check target's room or reject with TRIP_LIMIT_REACHED (needs user decision since docs state otherwise). Add tests for all three.

**H2. Apple refund of one period permanently revokes Pro, later paid renewals ignored (AC3).**
- migration:141 and :170 `revoked_at = coalesce(b.revoked_at, p_revoked_at)` keyed on `originalTransactionId`, which Apple keeps across every renewal and resubscription in the group. Probe: record Pro 7000 -> REFUND notification for transaction 7000 -> DID_RENEW transaction 7001 (+400 d) -> restore => `/v1/me/entitlements pro = null`, while POST /purchases/apple answers `active: true` (routers/billing.py:176 computes `active` from the store answer, not the row).
- Same mechanism hits REFUND_REVERSED (never un-revokes) and, likely, Google voided orders for a single renewal of a continuing subscription (billing.py:240-242 revokes the whole token).
- Fix: for `pro`, apply revocation per transaction: store latest transaction id/expiry; a revocation counts only if it covers the current period (revoked transaction's expiry >= stored expiry), and a later non-revoked transaction (newer `expiresDate`) clears `revoked_at`. Keep sticky revoke for `trip_pass`. Return `active` from the stored row (have `record_purchase` return revoked_at/expires_at). Add a test with refund-then-renew.

**H3. Purchases without an account id are claimable by whoever sends them first (AC1).**
- `modules/billing.py:167` refuses only when `purchase.account` is set and differs; `None` passes. Probe: JWS without `appAccountToken` recorded by an unrelated account => 200. Vectors: App Store offer codes / promoted IAP / Play Store resubscribe / promo codes (no app-set id), Family Sharing (if enabled on Pro), shared device with one Apple ID and several Beluno accounts, older app builds.
- Fix (needs product call): refuse `account is None` with a distinct code (e.g. `409 PURCHASE_UNBOUND`), or accept only for Pro and only when `inAppOwnershipType == PURCHASED`; also refuse `FAMILY_SHARED` unless intended. Document client handling.

## Medium

**M1. Google subscription replacement (linkedPurchaseToken) leaves the old token entitled; expiry can never shrink.**
`stores.py:283-301` ignores `linkedPurchaseToken`; migration:139-140/168-169 use `greatest()`. Account X buys Pro (T1); upgrade/resubscribe with obfuscated id Y yields T2 linked to T1; Y records T2 and X keeps Pro until T1's stored expiry: one payment, two Pro accounts. Fix: on recording a v2 subscription with `linkedPurchaseToken`, end the linked row (set expires to now / mark replaced) in the same definer call; let the store's latest answer set expiry (drop `greatest` or only keep it for Apple out-of-order notifications with a signedDate comparison).

**M2. Unauthenticated Google push endpoint triggers an outbound HTTPS fetch per request.**
`stores.py:238-241`: google-auth `verify_token` fetches Google certs **before** parsing the token, uncached, new `requests.Session` per call, default 120 s timeout, in the shared anyio thread pool (40). Any `Authorization: Bearer x` costs one outbound call + one thread; only per-IP limit 600/10 min (`rate_limits.py`). Fetch failure raises `TransportError` and wrong issuer raises `GoogleAuthError`, neither a ValueError => 500. Fix: reject structurally invalid JWTs locally (header `alg`=RS256, `kid` present) before any network; cache certs (e.g. `cachecontrol` session or PyJWT `PyJWKClient` with cache) with a short timeout; catch `google.auth.exceptions.GoogleAuthError` -> False / 503.

**M3. Error mapping: several 500s and a transient failure reported as permanent.**
- Production without `BELUNO_APPLE_APP_APPLE_ID`: `SignedDataVerifier` raises ValueError inside `Runtime.apple_store` (context.py:75) => probe returned 500. Missing root-cert path => FileNotFoundError 500. Fix: model validator in config.py (Production requires app id; root files exist), and/or map to StoreUnavailable.
- `stores.py:247-248` `credentials.refresh` RefreshError/TransportError escape => 500 (API) ; `response.json()` on non-JSON 200 => 500. Wrap as StoreUnavailable.
- OCSP outage raises `VerificationException(RETRYABLE_VERIFICATION_FAILURE)`; `stores.py:125-126` maps every VerificationException to `422 PURCHASE_INVALID` ("forged"). A client honoring the contract may give up / finish the transaction. Map status 7 to StoreUnavailable (503).

**M4. Receipt limit is check-then-insert without a lock.** `media.py:542` counts then inserts; `_plan` loads without FOR UPDATE. Two concurrent uploads at limit-1 both pass. Pre-existing, but now a paid limit. Fix: `pg_advisory_xact_lock(hashtextextended('media:receipts:'||plan_id,0))` before counting (same pattern as LOCK_TRIPS).

## Low
- L1. Apple notification about a product not sold here (`product_for` raises) answers 422; Apple retries for days. Return 204 for unknown products in notifications (`routers/billing.py:128-131`).
- L2. Google: `testPurchase` / `purchaseType==0` purchases are accepted in production (environment only stored); `products.get` body `productId`/`quantity` not checked (`stores.py:263-281`). License testers are developer-controlled, so low.
- L3. Definer functions are only as strong as the API: any `api_runtime` session can `record_purchase(..., p_expires_at => 'infinity')` for itself or `update_purchase` any known original_id. Inherent to "API verifies"; at least bound `p_expires_at` (e.g. <= now + 400 d), validate `p_store`/`p_product`, and don't claim stronger insider isolation in docs.
- L4. `apple_online_checks` can be set False in any environment (then chain validity uses the JWS `signedDate`). Refuse False when `apple_environment == "Production"`.
- L5. Worker ack: if the app (against docs) consumes/acknowledges itself, Google answers 400 -> PurchaseInvalid -> 10 failing attempts over ~24.6 h, row stays unacknowledged. On PurchaseInvalid, re-read state (`acknowledgementState`/`consumptionState`) and mark acknowledged.
- L6. Docs (billing.md "Buying and restoring"): `Transaction.currentEntitlements` excludes consumables; say to drain `Transaction.unfinished`/`Transaction.updates` at launch, and state whether to finish on 409 PURCHASE_USED/OWNED_ELSEWHERE and on 422 (otherwise loops or lost passes).
- L7. Two members can buy a pass for an already-unlocked trip (double charge). The server only sees the purchase after payment, so the app must check `/entitlement` before opening the store sheet; say so in the docs.
- L8. Test gaps: no positive/negative test of real `push_is_authentic` with a signed ID token (wrong email, email_verified false, wrong aud) — FakeGooglePlay bypasses it; no Apple other-environment case; no notification-for-other-bundle case; none of H1/H2/H3 scenarios.

## Edge cases checked and OK
- plan_unlock on a plan the actor is not in -> NULL (no leak); trips_counting scoped to actor; RLS SELECT own only; worker has no table grant.
- Concurrent record of the same token: FOR UPDATE + ON CONFLICT DO NOTHING (definer owner bypasses RLS) answers as replay; Trip Pass cannot move trips, also after purge (plan_id NULL -> PURCHASE_USED).
- Plan purge with a pass attached: purge succeeds, purchase row kept with plan_id NULL.
- No store I/O inside DB transactions (routes verify before `open_context`; worker does read txn -> I/O -> write txn). Job args carry purchase id only; queueing_lock collision handled by `defer_in_transaction`.
- Retry schedule: exp 3^(n+1), 10 attempts ~24.6 h (matches runbook "about a day").
- Limits off when settings unset; staging blanks normalized by `blank_means_unset`.
- Xcode/LocalTesting unreachable via `Literal["Sandbox","Production"]`.

## Recommended actions
1. H1: limit checks on restore, owner-based reopen, decision on transfer; tests.
2. H2: per-transaction revocation for Pro, clear on newer renewal; `active` from stored row; tests.
3. H3: decide policy for purchases without account id / family sharing; enforce.
4. M1-M4, then Low items.

## Plan follow-ups
Phase-06 entitlements work functionally present; not ready to mark done until H1-H3 fixed.

## Unresolved questions
1. Ownership transfer vs limit: should transfer to a free user at the limit be refused, or is the documented "not checked" accepted product behavior?
2. Purchases without appAccountToken/obfuscated id (offer codes, Play Store resubscribe, Family Sharing): refuse, or allow for Pro only? Is Family Sharing enabled on the Pro product?
3. Do Google voided-purchase notifications fire for a single refunded renewal order of a continuing subscription in our setup (would make H2 apply to Google too)?
4. Should other trip members learn of a newly bought pass via sync (no change_log/scope signal is emitted today; offline clients keep showing the limit)?

Status: DONE_WITH_CONCERNS
Summary: Core verification, RLS, and transaction hygiene are sound and gates pass, but the trip limit is bypassable (restore, admin reopen, transfer) and an Apple refund of one period permanently disables a renewing Pro; token-less purchases are claimable by any account.
Concerns/Blockers: H1-H3 contradict acceptance criteria 1-3; H3 and transfer handling need a product decision.
