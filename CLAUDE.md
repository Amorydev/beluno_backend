# Beluno backend: working notes for coding sessions

Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL with RLS, Procrastinate, managed with uv.
The repository root is the backend. Talk to the user in Vietnamese; code, comments, commits,
and docs stay in English.

## Read before changing anything

- `README.md`, `docs/adr/` (especially 0004 offline sync and 0007 self-hosted identity),
  `docs/contracts/`, `docs/architecture/`.
- `plans/261005-2356-beluno-backend-plan/plan.md` and the current phase file, including its
  "Execution Decisions" and "Completion Notes".

## Fixed decisions (ask the user before reversing any of them)

- No Supabase. The API is the identity provider: Google/Apple ID tokens, email OTP plus magic
  link, guests minted by invites. ES256 access JWTs live 15 minutes; refresh tokens are opaque,
  rotate on use, and reuse revokes the session. Clients refresh single-flight.
- Never link accounts by matching email: return `409 ACCOUNT_LINK_REQUIRED`; the user links
  explicitly with a fresh step-up.
- Placeholders can only be member or viewer; a guest who claims a placeholder gets the guest role.
- Modular monolith on standard PostgreSQL; object storage will be S3-compatible.

## Request and mutation pattern

- Every request runs inside `open_context` (`src/beluno/modules/context.py`): one transaction,
  `app.actor_id` set, and the session re-checked in the database.
- Authorize with `load_plan`/`load_group` plus `require_*`; decisions live in the pure policy
  `src/beluno/authorization/policy.py`.
- Every mutation goes through `record_mutation` (`src/beluno/modules/sync_audit/recorder.py`),
  which writes `audit_events` and `change_log` in the same transaction. Extend that seam rather
  than writing audit or sync rows elsewhere.

## Database traps

- Tables with RLS: never use `INSERT ... RETURNING` or `ON CONFLICT`; PostgreSQL checks the SELECT
  policy against the new row before the actor can see it. Set every value (UUIDv7 via `new_id()`,
  timestamps) in Python; models declare no `server_default`. For uniqueness races, do a plain
  INSERT inside `session.begin_nested()` and catch `IntegrityError`.
- `SELECT ... FOR UPDATE` also applies the UPDATE policy. Keep the unlocked fallback
  (`_select_visible` in `src/beluno/authorization/access.py`) so 403 stays distinct from 404.
- Migrations are forward-only and numbered (`000007_...` next). RLS, triggers, grants, and
  SECURITY DEFINER functions (owned by migrator, `SET search_path = pg_catalog, pg_temp`) are raw
  SQL inside the migration. The header states forward action, lock/scan risk, validation query,
  compatibility, and rollback. New functions: `REVOKE EXECUTE ... FROM PUBLIC`, then grant only
  the roles that call them.
- Write-guard triggers (`000003`) restrict insiders. A new write path must use only transitions
  the guards allow, or extend the guards in a new migration.
- Enqueue jobs inside the domain transaction with `defer_in_transaction`
  (`src/beluno/worker/enqueue.py`). Job arguments carry IDs only, never secrets.
- No external I/O while a transaction is open. Rate-limit hits commit in their own transaction.

## Quality gates (all must pass)

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src                      # strict
uv run coverage run -m pytest && uv run coverage report --fail-under=80
```

- Integration, security, and e2e suites need real PostgreSQL: Testcontainers when Docker works,
  otherwise a local server plus
  `BELUNO_TEST_ADMIN_DATABASE_URL=postgresql://postgres:<pw>@localhost:5432/postgres`.
  A run where those suites are skipped en masse does not count.
- When the API changes, run `uv run python scripts/export_openapi.py` and commit
  `openapi/openapi.json`; `scripts/check_openapi_compatibility.py` runs on every PR.
- Do not mock our own code. No vacuous tests (asserts inside `if`, `pytest.skip` on setup
  failure). Re-verify any subagent report yourself.

## Git

- Conventional commits in English, no Co-Authored-By or other attribution.
- No phase numbers or plan codes in commit messages, code comments, or test names.
- Stage explicit paths. Work on a feature branch, open a PR to `main`, wait for green CI, and
  never push to `main` directly.
