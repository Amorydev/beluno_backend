## Code Review Summary: Report a problem (feat/problem-reports, uncommitted)

### Scope
- Files: alembic/versions/000015_problem_reports.py, src/beluno/{db/models,contracts,modules,api/routers}/support.py, api/main.py, modules/account_deletion.py, modules/iam/rate_limits.py, testkit/{database,tenants}.py, scripts/support.py, tests/integration/test_support.py, README, data-model, retention-matrix, phase-06
- LOC: ~650 new (excluding openapi.json +430)
- Gates (run by reviewer): ruff check/format OK, mypy strict OK, full suite on Testcontainers **609 passed** (no mass skips), total coverage 95%; support module 94% (missed: 105->127, 123-126, 194->209). OpenAPI regenerated output identical to committed file; `check_openapi_compatibility.py main -> working tree` rc=0.
- Live probes (scratchpad, Testcontainers): NUL in message, code-collision retry, merged-guest deletion, voided expense link.

### Overall Assessment
Solid, small slice. Definer functions, RLS, IDOR, and retry are correct. One verified retention defect (merged guest reports survive account deletion) and one verified 500 (NUL in message). Rest is low-severity.

### Critical Issues
None.

### High Priority

**H1. A merged guest's reports survive account deletion** (verified)
- `alembic/versions/000015_problem_reports.py:97` deletes only `user_id = iam.actor_id()`. Guest merge (`modules/iam/users.py:200` `retire_merged_guest`) keeps the guest user row with `merged_into_user_id = account`; reports filed as the guest keep `user_id = guest`. Nothing transfers them (unlike `coordination.transfer_private_packing`).
- Probe: guest files report "guest words" -> signs in with Google + `merge_guest_participations` -> `DELETE /v1/me` 204 -> row still present: `[('guest words', {'id': <guest>, 'guest': True})]`.
- Breaks retention-matrix row ("until the person deletes their account") and the account-deletion docstring. The existing precedent `iam.forget_merged_guests` (000009:257) follows `merged_into_user_id`.
- Fix (000015 is unreleased, edit in place): 
  ```sql
  DELETE FROM analytics_ops.problem_reports
  WHERE user_id = iam.actor_id()
     OR user_id IN (SELECT id FROM iam.users WHERE merged_into_user_id = iam.actor_id());
  ```
  Add an integration test mirroring `test_guests_merged_into_the_account_lose_their_name_too`.

### Medium Priority

**M1. NUL byte in `message` -> 500** (verified)
- `src/beluno/contracts/support.py:23-25` `Observation` re-implements text validation and skips `reject_control_characters` (`contracts/common.py:14`). Probe: `"hi\x00there"` -> psycopg `DataError: text fields cannot contain NUL` -> unhandled, `beluno.api` logs "unhandled error", 500 (after the rate-limit hit was spent). ESC/other C0 controls are stored as-is.
- Fix: reuse the shared pattern, e.g. `Annotated[str, AfterValidator(strip_optional_text), StringConstraints(max_length=1000)]` (as `LongText` does). Add a 422 test.

**M2. Operators read reports with `worker_runtime`; no access trail**
- `000015:70-73` grants SELECT on users' free text + diagnostics to the job-processing role, so any job/worker compromise reads every report. The platform already defines `support_readonly` ("time-bound audited access to explicitly safe views", `sql/platform-runtime-grants.sql:17`, phase-02 plan). `scripts/jobs.py retry` takes `--operator` for audit; `scripts/support.py` reads PII with none.
- User decision "operators read via script" is untouched; the role choice is not part of it. Options: (a) keep worker role, add `--operator` + `record_audit`-style row per `show`; (b) grant SELECT to `support_readonly` and run the script under a login role that holds it. Ask user.

### Low Priority

- **L1. Migration header inaccurate**: `000015:23` says "No existing table is touched", but `user_id REFERENCES iam.users (id)` takes a brief SHARE ROW EXCLUSIVE lock on `iam.users` (000014 header lists exactly this kind of lock). Fix the header text.
- **L2. `/v1/support/checks` has no rate limit**; each call runs `finance.reconcile_plan` (full aggregate over the plan's postings, transactions, accounts; `ledger_postings_plan_idx` exists). Cost is bounded per plan and comparable to other ledger GETs; acceptable, but if large plans appear, reuse a per-user read limit.
- **L3. Snapshot `record.deleted` is always False for expenses/settlements**: `modules/support.py:215` reads `deleted_at`, which Expense/Settlement lack (`voided_at`, `reversed_at`). Probe on a voided expense: `{'state': 'voided', 'deleted': False}`. Misleads operators; compute from `voided_at`/`reversed_at` too or drop `deleted`.
- **L4. Client-supplied free text in the snapshot**: `AppVersion` (`contracts/support.py:26`) allows letters and spaces up to 64 chars, and `session.app_version` (sign-in contract: free text, 32 chars, no pattern) is copied into `diagnostics.session`. The "no names" promise relies on the app. Tighten to `^[0-9A-Za-z.+\-_()]{1,32}( \([0-9]+\))?$`-style or accept.
- **L5. `ledger_ok`** (`modules/support.py:88`) maps NULL (not an active participant, only reachable in a race after `require_plan`) to False ("balance looks wrong"). Negligible.
- **L6. Account export omits the person's own reports** (`exports.account_json`). If the export is meant to be the data-access answer, reports belong there. Product question.
- **L7. `scripts/support.py --limit`** accepts negatives/huge values (PG errors on negative LIMIT). Trivial.

### Edge Cases Found by Scout (verified OK)
- Definer `finance.actor_ledger_problems`: `SET search_path = pg_catalog, pg_temp`, fully qualified calls, REVOKE PUBLIC + api-only grant, active-participant gate before count; non-participants hit `load_plan` 404 first, so no count/timing leak. STABLE wrapper over STABLE `reconcile_plan`: no write conflict.
- `forget_problem_reports`: definer, qualified, api-only grant, scoped to actor (see H1).
- RLS: INSERT `WITH CHECK (user_id = iam.actor_id())`, no api SELECT policy or grant (test asserts InsufficientPrivilege). Plain INSERT, no RETURNING (`expire_on_commit=False`, all values set in Python).
- IDOR: `_record` uses RLS-filtered `session.get` + `plan_id` equality; other plan's record and another user's private packing item both 422 identically to nonexistent. VIEW requires active participant (`policy.decide_plan`), so group-visible non-participants get 404.
- Retry: probe forced a duplicate code -> second insert retried inside `ctx.savepoint()`, rolled-back object expunged, new object, 2 rows, 2 audit events. Exhaustion re-raises (500) at ~1/1.1e12 odds; fine.
- JSONB `none_as_null=True` keeps the `(diagnostic_code IS NULL) = (diagnostics IS NULL)` CHECK satisfied; pydantic strip+1..1000 is stricter than DB `btrim` CHECK; linked-needs-plan enforced in contract and DB.
- Rate limit placed before `open_context` in its own transaction (same as exports).
- Snapshot keys: IDs, enums, versions, seqs, release, auth_method/platform/app_version; no device_label, display names, expense text, notes, booking secrets.
- Audit `support.problem_reported`: metadata category + bool only; audit_events unreadable by API.
- `analytics_ops` USAGE already granted by `sql/platform-runtime-grants.sql`.

### Test Quality / Gaps
- Retry branch (lines 123-126) untested; no-plan-with-diagnostics snapshot (194->209) untested.
- Privacy assertions are negative spot checks ("Yakiniku", "Ann" absent). Prefer an exact allowlist of snapshot keys so a future added field fails the test.
- Missing: merged-guest deletion (H1), NUL/control chars (M1), stranger on `/checks` -> 404, insider INSERT with a foreign `user_id` rejected by WITH CHECK.

### Positive Observations
- Correct use of `ctx.savepoint()` (not raw `begin_nested`) and `UniqueViolation`-only retry; definer pattern matches repo conventions exactly.

### Recommended Actions
1. H1: extend `forget_problem_reports` to merged guests + test.
2. M1: reuse `strip_optional_text` for `message` + 422 test.
3. M2: ask user: worker role + operator audit vs `support_readonly`.
4. L1/L3 quick fixes; add retry and snapshot-allowlist tests.

### Metrics
- Type coverage: mypy strict clean
- Test coverage: 95% total; support module 94%, router/contracts 100%
- Lint issues: 0

### Unresolved Questions
1. Operator access role/audit (M2): keep worker_runtime or move to support_readonly?
2. Should "ENTRY VERIFIED" mean more than "record exists in plan" (e.g. the expense's current revision has a posted ledger transaction)? Today only plan-wide `ledger_ok` + `record_found`.
3. Should account export include the person's reports (L6)?
4. Retention limit for reports of active accounts ("or a legal policy sets a limit") is open-ended.

Status: DONE_WITH_CONCERNS
Summary: Slice is sound on RLS, definer safety, IDOR, and retry; one verified retention defect (merged-guest reports survive account deletion) and one verified 500 (NUL in message) should be fixed before merge.
Concerns/Blockers: M2 needs a user decision on operator role; reviewer ran `scripts/export_openapi.py` once (output byte-identical to the working-tree file, no change).
