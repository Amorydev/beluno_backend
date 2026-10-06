# Permission Matrix

Authorization is decided server-side on every request from the caller's
**current** relationships loaded from PostgreSQL (never from token claims):

```text
actor kind (registered | guest) x current plan participant state x role
x plan state x step-up freshness x action
```

The policy (`src/beluno/authorization/policy.py`) defaults to deny. A caller with
no viewing relationship receives `404 NOT_FOUND` so existence is never confirmed;
a caller who can see the resource but may not act receives `403 FORBIDDEN`, and
`403 STEP_UP_REQUIRED` when a recent sign-in is needed (session
`authenticated_at` older than `BELUNO_AUTH_STEP_UP_MAX_AGE_SECONDS`, default
10 minutes). PostgreSQL RLS repeats tenant scoping as defense in depth.

The tables below are executable: `tests/security/test_permission_matrix.py`
parses them and checks every cell against the policy code, then sweeps every
combination of role, access state, plan state, deletion, guest, and step-up
state for invariant violations.

Cell values: `allow`, `deny`, `step-up` (allowed only after a recent sign-in).

## Plan actions

Columns are the caller's active participant role; `outsider` is anyone who is
not an active participant (they get `404`).
The `guest` role belongs to guest identities, which never pass
"registered-only" actions.

<!-- plan-matrix:start -->
| Action | owner | admin | member | viewer | guest | outsider |
|---|---|---|---|---|---|---|
| plan.view | allow | allow | allow | allow | allow | deny |
| plan.update | allow | allow | deny | deny | deny | deny |
| plan.state.change | allow | allow | deny | deny | deny | deny |
| plan.delete | step-up | deny | deny | deny | deny | deny |
| plan.duplicate | allow | allow | deny | deny | deny | deny |
| plan.participants.view | allow | allow | allow | allow | allow | deny |
| plan.participants.add | allow | allow | deny | deny | deny | deny |
| plan.participants.remove | allow | allow | deny | deny | deny | deny |
| plan.participants.change_role | allow | allow | deny | deny | deny | deny |
| plan.participants.review | allow | allow | deny | deny | deny | deny |
| plan.ownership.transfer | step-up | deny | deny | deny | deny | deny |
| plan.leave | allow | allow | allow | allow | allow | deny |
| plan.rsvp.respond | allow | allow | allow | allow | allow | deny |
| plan.invites.manage | allow | allow | deny | deny | deny | deny |
| plan.finance.view | allow | allow | allow | allow | allow | deny |
| plan.expenses.create | allow | allow | allow | deny | allow | deny |
| plan.expenses.manage | allow | allow | deny | deny | deny | deny |
| plan.settlements.record | allow | allow | allow | deny | allow | deny |
| plan.settlements.manage | allow | allow | deny | deny | deny | deny |
| plan.settlements.answer | allow | allow | allow | allow | allow | deny |
| plan.budgets.manage | allow | allow | deny | deny | deny | deny |
| plan.fund.contribute | allow | allow | allow | deny | allow | deny |
| plan.fund.manage | allow | allow | deny | deny | deny | deny |
| plan.ledger.adjust | step-up | deny | deny | deny | deny | deny |
<!-- plan-matrix:end -->

State narrowing (applies on top of the table):

- Content edits (`plan.update`, participant add/review, invites) require the plan to be `draft`, `planning`, `active`, or `settling`; RSVP
  requires `draft`, `planning`, or `active`. `completed`, `archived`, and
  `cancelled` plans are read-only except state changes, duplication, and
  deletion.
- Finance writes (expenses, budgets, fund, adjustments) require `draft`,
  `planning`, `active`, or `settling`; settlements and waivers also accept
  `completed`, because people pay each other back after the plan is over.
- Row rules on top of the table: an expense is revised, voided, or refunded by
  its creator (who must still hold `plan.expenses.create`) or by anyone with
  `plan.expenses.manage`; naming the fund as a payer also needs
  `plan.fund.manage` or being the fund's custodian. A settlement is recorded by
  one of its two parties or by a manager. The creditor (the participant the
  money now belongs to after merges) confirms or disputes it with
  `plan.settlements.answer`; managers answer for creditors who are placeholders
  or no longer active, never for a settlement they owe themselves. Its recorder
  or a manager reverses it, and the creditor may reverse one they never
  confirmed. An account that claimed a guest account counts as the creator or
  recorder of the records that guest made. A waiver is given by the creditor
  (managers for placeholder or inactive creditors, never when they are the
  debtor) and never exceeds what the debtor owes the group and the creditor is
  owed. Participants contribute
  to the fund for themselves; contributions for others, withdrawals, and fund
  settings need `plan.fund.manage`.
- Finance is private to the plan's active participants.
- While deletion is scheduled only reads, leaving, and `plan.delete`
  (restore) are allowed.
- `pending_approval`, `left`, `removed`, and `merged` participants have no
  rights: the plan is hidden from them. Removed participants keep their row
  and history.
- Owners cannot leave (`409 OWNER_TRANSFER_REQUIRED`); ownership moves only by
  transfer to an active registered participant.
- Placeholders can only be members or viewers; a guest who claims a
  placeholder receives the `guest` role, so bearer links never grant
  management rights. Invite links stop admitting people once a plan is
  `completed`, `archived`, `cancelled`, or scheduled for deletion, and claim
  links stop working once the placeholder is removed.
- Target rules: owners manage every non-owner; admins manage only members,
  viewers, and guests and may only assign `member` or `viewer`; only owners
  create admin invites; guests cannot be given another role or become admins.

## Invite links

| Operation | Who |
|---|---|
| Preview | anyone holding the token (minimal fields only) |
| Redeem plan join invite | registered users; guests when `allow_guests`; email-bound invites only the intended verified email |
| Redeem placeholder claim invite | anyone holding the single-use token; existing participants must confirm a merge |
| Create / list / revoke / rotate | `plan.invites.manage` |

## Database write guards

RLS selects the rows a runtime role may touch; BEFORE triggers
(`alembic/versions/000003_tenant_write_guards.py`) limit what an insider may
change on them: tenant keys are immutable, only active owners/admins change
other people's rows or the plan, only the owner moves ownership, a
non-manager changes only their own row through the transitions the API offers,
and an invite-token holder can only count one use.

## Background roles

| Role | Access |
|---|---|
| `api_runtime` | DML on module tables under RLS; insert-only audit events; change rows only through `sync_audit.append_changes`/`read_changes` (visibility checked per scope); own operation records; scope heads readable for visible scopes; enqueue jobs |
| `worker_runtime` | email challenge delivery, auth record purge, ledger reconciliation; insert-only audit; `append_changes`; retention gates `compact_changes`/`purge_operations`; job queue tooling |
| `scheduler_runtime` | job queue only |
| `migrator` | owns tables and SECURITY DEFINER policy helpers; never used by application traffic |
