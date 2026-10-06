# Permission Matrix

Authorization is decided server-side on every request from the caller's
**current** relationships loaded from PostgreSQL (never from token claims):

```text
actor kind (registered | guest) x current group membership x current plan
participant state x role x plan/group state x visibility x step-up freshness x action
```

The policy (`src/beluno/authorization/policy.py`) defaults to deny. A caller with
no viewing relationship receives `404 NOT_FOUND` so existence is never confirmed;
a caller who can see the resource but may not act receives `403 FORBIDDEN`, and
`403 STEP_UP_REQUIRED` when a recent sign-in is needed (session
`authenticated_at` older than `BELUNO_AUTH_STEP_UP_MAX_AGE_SECONDS`, default
10 minutes). PostgreSQL RLS repeats tenant scoping as defense in depth.

The tables below are executable: `tests/security/test_permission_matrix.py`
parses them and checks every cell against the policy code, then sweeps every
combination of role, access state, plan state, visibility, deletion, guest, and
step-up state for invariant violations.

Cell values: `allow`, `deny`, `step-up` (allowed only after a recent sign-in).

## Plan actions

Columns are the caller's active participant role; `group-member` is an active
member of the plan's group who is not a participant of a group-visible plan.
The `guest` role belongs to guest identities, which never pass
"registered-only" actions.

<!-- plan-matrix:start -->
| Action | owner | admin | member | viewer | guest | group-member |
|---|---|---|---|---|---|---|
| plan.view | allow | allow | allow | allow | allow | allow |
| plan.update | allow | allow | deny | deny | deny | deny |
| plan.state.change | allow | allow | deny | deny | deny | deny |
| plan.delete | step-up | deny | deny | deny | deny | deny |
| plan.duplicate | allow | allow | deny | deny | deny | deny |
| plan.join | deny | deny | deny | deny | deny | allow |
| plan.participants.view | allow | allow | allow | allow | allow | allow |
| plan.participants.add | allow | allow | deny | deny | deny | deny |
| plan.participants.remove | allow | allow | deny | deny | deny | deny |
| plan.participants.change_role | allow | allow | deny | deny | deny | deny |
| plan.participants.review | allow | allow | deny | deny | deny | deny |
| plan.ownership.transfer | step-up | deny | deny | deny | deny | deny |
| plan.leave | allow | allow | allow | allow | allow | deny |
| plan.rsvp.respond | allow | allow | allow | allow | allow | deny |
| plan.invites.manage | allow | allow | deny | deny | deny | deny |
| plan.travel.view | allow | allow | allow | allow | allow | allow |
| plan.travel.manage | allow | allow | allow | deny | deny | deny |
<!-- plan-matrix:end -->

State narrowing (applies on top of the table):

- Content edits (`plan.update`, participant add/review, invites, travel edits)
  require the plan to be `draft`, `planning`, `active`, or `settling`; RSVP
  requires `draft`, `planning`, or `active`. `completed`, `archived`, and
  `cancelled` plans are read-only except state changes, duplication, and
  deletion.
- While deletion is scheduled only reads, leaving, and `plan.delete`
  (restore) are allowed.
- `pending_approval`, `left`, `removed`, and `merged` participants have no
  rights: the plan is hidden unless it is group-visible to an active group
  member. Removed participants keep their row and history.
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

## Group actions

<!-- group-matrix:start -->
| Action | owner | admin | member | invited |
|---|---|---|---|---|
| group.view | allow | allow | allow | allow |
| group.update | allow | allow | deny | deny |
| group.delete | step-up | deny | deny | deny |
| group.members.view | allow | allow | allow | deny |
| group.members.add | allow | allow | deny | deny |
| group.members.remove | allow | allow | deny | deny |
| group.members.change_role | allow | deny | deny | deny |
| group.ownership.transfer | step-up | deny | deny | deny |
| group.invitation.respond | deny | deny | deny | allow |
| group.leave | allow | allow | allow | deny |
| group.plans.create | allow | allow | allow | deny |
<!-- group-matrix:end -->

- `group.members.add` also governs group invite links; admins may add members
  but not admins, and may remove plain members only.
- While deletion is scheduled only view, member list, leave, and
  `group.delete` (restore) are allowed.
- Guests cannot hold group membership, create groups, or create plans.

## Plan series

A series is visible to its creator and to active members of its group. Only
the creator (a registered user) may split or cancel it, because the creator
owns every occurrence it materializes. Occurrences are ordinary plans governed
by the plan table above.

## Invite links

| Operation | Who |
|---|---|
| Preview | anyone holding the token (minimal fields only) |
| Redeem plan join invite | registered users; guests when `allow_guests`; email-bound invites only the intended verified email |
| Redeem placeholder claim invite | anyone holding the single-use token; existing participants must confirm a merge |
| Redeem group invite | registered users only |
| Create / list / revoke / rotate | `plan.invites.manage` or `group.members.add` |

## Database write guards

RLS selects the rows a runtime role may touch; BEFORE triggers
(`alembic/versions/000003_tenant_write_guards.py`) limit what an insider may
change on them: tenant keys are immutable, only active owners/admins change
other people's rows or the plan/group, only the owner moves ownership, a
non-manager changes only their own row through the transitions the API offers,
and an invite-token holder can only count one use.

## Background roles

| Role | Access |
|---|---|
| `api_runtime` | DML on module tables under RLS; insert-only on audit/change log; enqueue jobs |
| `worker_runtime` | email challenge delivery, auth record purge, series materialization acting as the series creator; insert-only audit |
| `scheduler_runtime` | job queue only |
| `migrator` | owns tables and SECURITY DEFINER policy helpers; never used by application traffic |
