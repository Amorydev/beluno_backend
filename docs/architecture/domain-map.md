# Domain Map

```text
User ──< Crew ──> User (member_user_ids, private to the owner)

User ──< Plan ──< PlanParticipant (trips and hangouts)
              ├── PlanInvite (join or claim placeholder)
              ├── planning and coordination
              ├── finance
              └── media and memories
```

- **User**: registered or guest; guests are upgraded when they claim an identity. Users have an optional default currency for new plans.
- **Crew**: private list of 1–50 registered people, owned by one registered user. A newly listed person must currently be active in a plan with the owner. Guests are never listed; they join plans through invite links.
- **Plan**: a trip (destination-based, up to 10 stops) or hangout (activity-based, optional icon); fixed type at creation. State machine: draft → planning → active → settling → completed.
- **PlanParticipant**: stable historical identity for votes, tasks, money, and RSVP; guest claims link participants to users without rewriting history. Members hold a default share (default 1.0×), avatar color, and optional capability grants.
- **PlanInvite**: bearer tokens for joining or claiming an existing placeholder; optional email binding, use limit, and guest switch.
- **Finance**: the plan ledger (expenses, refunds, settlements, budgets, cost commitments, virtual fund). Other modules record planned costs only through `CostCommitmentPort`; participant merges call the finance merge port inside the claim transaction.

Modules communicate through application ports or transactional events, never by
ad-hoc access to another module's tables. Domain writes are transactional: they
record audit and change rows through `record_mutation` (written at commit with
per-scope sequencing) and enqueue jobs in the same transaction.
