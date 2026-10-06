# Domain Map

```text
User ──< Group ──< GroupMembership
                └─< GroupInvite ──< Redeem
                
User ──< PlanSeries ──< Plan ──< PlanParticipant
                              ├── PlanInvite (join or claim)
                              ├── planning and coordination
                              ├── finance
                              └── media and memories
```

- **User**: registered or guest; guests are upgraded when they claim an identity.
- **Group**: reusable social container; members, admins, owners; invites grant membership roles.
- **PlanSeries**: recurrence rule and participants; generates plan instances on a schedule.
- **Plan**: individual dinner, coffee, sport, birthday, outing, or trip; state machine (draft → planning → active → settling → completed).
- **PlanParticipant**: stable historical identity for votes, tasks, money, and RSVP; guest claims link participants to users without rewriting history.
- **Invites**: reusable group invites (membership) and plan invites (join or claim existing participant).

Modules communicate through application ports or transactional events, never by
ad-hoc access to another module's tables. Domain writes are transactional and
enqueue audit and outbox events in the same transaction.
