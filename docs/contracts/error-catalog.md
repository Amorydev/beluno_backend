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
| `ALREADY_MEMBER` | 409 | Person already belongs to the group |
| `ALREADY_PARTICIPANT` | 409 | Person already participates in the plan |
| `GUEST_NOT_ALLOWED` | 409 | Guests cannot be added this way; they join through an invite |
| `IDENTITY_ALREADY_LINKED` | 409 | The external identity belongs to another account |
| `INVALID_STATE_TRANSITION` | 409 | Action is not valid in the resource's current state |
| `OWNER_TRANSFER_REQUIRED` | 409 | Owner must transfer ownership before leaving |
| `PARTICIPANT_MERGE_REQUIRED` | 409 | Claim would merge with an existing participation; resend with consent |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same `Idempotency-Key` or `operation_id` was sent with a different request |
| `OPERATION_SKIPPED` | 409 (push item only) | The operation was not attempted: an earlier operation on the same scope must be retried first, or a dependency was not applied |
| `VERSION_CONFLICT` | 412 | `expected_version`/`If-Match` is stale; `current` carries the canonical representation |
| `REQUEST_TOO_LARGE` | 413 | Body or push batch exceeds the configured size limit |
| `VALIDATION_FAILED` | 422 | Request cannot satisfy a public contract |
| `CLIENT_UPGRADE_REQUIRED` | 426 | Sync protocol or command schema version is outside the supported window |
| `PRECONDITION_REQUIRED` | 428 | Update requires an `If-Match` header |
| `RATE_LIMITED` | 429 | Abuse limit reached; honour `Retry-After` |
| `FEATURE_DISABLED` | 503 | Entry point or command disabled by an operational kill switch |
| `RETRY_LATER` | 503 | Transient database conflict after bounded retries; nothing changed, resend unchanged after `Retry-After` |

Sync pull never reports a stale cursor as an HTTP error: the per-scope status
`resync_required` replaces the earlier `410 FULL_RESYNC_REQUIRED` design, and
`unavailable` means the scope is not the caller's to read.
