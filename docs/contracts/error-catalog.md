# Error Catalog

Public errors use `application/problem+json` with a stable uppercase `code`.
Validation failures list offending field paths in `details` without echoing
submitted values.

| Code | HTTP | Meaning |
|---|---:|---|
| `AUTHENTICATION_REQUIRED` | 401 | Credential missing, invalid, expired, revoked, or an email code that does not verify |
| `AUTHENTICATION_UNAVAILABLE` | 503 | Token signing/verification or a sign-in provider is not configured or unreachable |
| `FORBIDDEN` | 403 | Current relationship does not permit the action |
| `STEP_UP_REQUIRED` | 403 | Sensitive action needs a sign-in within the step-up window |
| `REAUTHENTICATION_MISMATCH` | 403 | Step-up used an identity that belongs to a different account |
| `INVITE_EMAIL_MISMATCH` | 403 | Invite is bound to a different verified email address |
| `NOT_FOUND` | 404 | Resource is absent or intentionally undisclosed |
| `INVITE_UNAVAILABLE` | 404 | Invite token is unknown, expired, revoked, or used up (never distinguished) |
| `ACCOUNT_LINK_REQUIRED` | 409 | Identity's verified email belongs to an existing account; link it from that account |
| `ALREADY_EXISTS` | 409 | A client-generated ID is already taken |
| `ALREADY_PARTICIPANT` | 409 | Person already participates in the plan |
| `GUEST_NOT_ALLOWED` | 409 | Guests cannot be added this way; they join through an invite |
| `IDENTITY_ALREADY_LINKED` | 409 | The external identity belongs to another account |
| `INVALID_STATE_TRANSITION` | 409 | Action is not valid in the resource's current state |
| `OWNER_TRANSFER_REQUIRED` | 409 | Owner must transfer ownership before leaving |
| `PARTICIPANT_MERGE_REQUIRED` | 409 | Claim would merge with an existing participation; resend with consent |
| `WAIVER_EXCEEDS_DEBT` | 409 | A waiver would forgive more than the debtor owes and the creditor is owed in that currency |
| `REFUND_EXCEEDS_AMOUNT` | 409 | Refunds would exceed the expense's current amount (also when a revision goes below them) |
| `FUND_INSUFFICIENT` | 409 | A fund-paid expense, withdrawal, or adjustment would overdraw the plan fund in that currency |
| `NOT_AVAILABLE_FOR_HANGOUT` | 409 | Budgets, cost commitments, and the kitty (settings, contributions, withdrawals, counts, fund-paid expenses, refunds to the fund) are for trips only |
| `LEDGER_CHANGED` | 409 | A confirmation named an older `ledger_seq`; review the latest entries and confirm again |
| `FUND_NOT_EMPTY` | 409 | The kitty still holds money in a currency to consolidate; pay it out first |
| `CONSOLIDATION_SETTLED` | 409 | A payment or waiver still in effect was recorded after this consolidation; reverse it first or keep the base-currency balances |
| `BASE_CURRENCY_CHANGED` | 409 | A rate named a base currency the plan has since moved away from; refresh and send a rate to the current one |
| `CONSOLIDATION_OPEN` | 409 | An active consolidation exists and the ledger is not settled; settle up or reverse that consolidation before changing the base currency |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same `Idempotency-Key` or `operation_id` was sent with a different request |
| `OPERATION_SKIPPED` | 409 (push item only) | The operation was not attempted: an earlier operation on the same scope must be retried first, or a dependency was not applied |
| `VERSION_CONFLICT` | 412 | `expected_version`/`If-Match` is stale; `current` carries the canonical representation |
| `REQUEST_TOO_LARGE` | 413 | Body or push batch exceeds the configured size limit |
| `VALIDATION_FAILED` | 422 | Request cannot satisfy a public contract |
| `AMOUNT_OUT_OF_RANGE` | 422 | An amount is zero, negative, above `10^12` minor units, or a conversion would exceed that bound |
| `CURRENCY_NOT_SUPPORTED` | 422 | Currency code is not in `GET /v1/currencies` (or is retired) |
| `SPLIT_INVALID` | 422 | Split, payer, or refund shares are inconsistent (sums, duplicates, weights, item totals) |
| `FX_RATE_INVALID` | 422 | Exchange rate is not positive, above `10^9`, or has more than 12 decimals |
| `PARTICIPANT_NOT_ELIGIBLE` | 422 | A named participant is unknown, from another plan, merged, or not active for this entry (never distinguished) |
| `LEDGER_ENTRY_UNBALANCED` | 422 | Adjustment entries do not sum to zero |
| `CLIENT_UPGRADE_REQUIRED` | 426 | Sync protocol or command schema version is outside the supported window |
| `PRECONDITION_REQUIRED` | 428 | Update requires an `If-Match` header |
| `RATE_LIMITED` | 429 | Abuse limit reached; honour `Retry-After` |
| `INTERNAL_ERROR` | 500 | Unexpected server failure; the response echoes nothing about the request beyond its request id |
| `FEATURE_DISABLED` | 503 | Entry point or command disabled by an operational kill switch |
| `RETRY_LATER` | 503 | Transient database conflict after bounded retries; nothing changed, resend unchanged after `Retry-After` |

Sync pull never reports a stale cursor as an HTTP error: the per-scope status
`resync_required` replaces the earlier `410 FULL_RESYNC_REQUIRED` design, and
`unavailable` means the scope is not the caller's to read.

Finance errors never echo amounts, descriptions, or notes. A deferred ledger
invariant that fails at commit (a server defect, not a client error) aborts the
whole transaction and surfaces as `500 INTERNAL_ERROR`; nothing is written.
Text fields reject control characters (NUL and friends) with `422`.
