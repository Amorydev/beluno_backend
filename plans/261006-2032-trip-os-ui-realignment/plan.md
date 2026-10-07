---
title: "Realign the backend to the Trip OS UI"
description: "Reshape the backend around trips, hangouts, and crews as designed in Stitch, close the money gaps, and ship in releases."
status: in-progress
priority: P1
tags: [backend, realignment, trips, hangouts, crews, ledger, offline-sync]
blockedBy: []
blocks: []
supersedes: "261005-2356-beluno-backend-plan phases 6-8"
created: 2026-10-06
---

# Realign the backend to the Trip OS UI

## Why

The Stitch UI (77 screens, DESIGN.md v3) and its spec `~/juntro/docs/trip-os-product-blueprint.md` describe a trip-first app: trips, light hangouts, and crews of people. The backend generalised that into durable groups, recurring plans, and a travel extension the UI never uses, and it misses a few money behaviours the UI depends on. Gap analysis: [ui-backend-gap-analysis report](../reports/ui-backend-gap-analysis-261006-2021-stitch-ui-vs-backend-report.md).

Decision (user, 2026-10-06): **realign, do not rebuild.** Platform, identity, offline sync, and the finance ledger stay. The app never moves money; settlements and the kitty are records only.

## Agreed defaults

- Remove groups (as containers), plan series, travel details/segments, and group visibility now; the app has not integrated them.
- Keep the `plan` name in the API; a plan has `type` `trip` or `hangout`.
- Settle per currency **and** offer "all in base currency" at frozen rates.
- Budget categories stay a fixed list for now.
- Release in stages. **Release 1:** trip + hangout + money + offline + invites + people/crews + activity. **Release 2:** planning tab. **Release 3:** media, push, export, recap, monetization.

## Phases

| Phase | Release | Dependency | Estimate | Name | Status |
|---:|---|---|---:|---|---|
| 1 | 1 | PR #4 fixes | 1.5–2 weeks | [Core realignment](./phase-01-core-realignment.md) | completed |
| 2 | 1 | 1 | ~2 weeks | [Money alignment](./phase-02-money-alignment.md) | pending |
| 3 | 1 | 1, 2 | 1–1.5 weeks | [People, activity, and account lifecycle](./phase-03-people-activity-account.md) | pending |
| 4 | 1 | 1–3 | 1–2 weeks + soak | [Release 1 hardening](./phase-04-release-one-hardening.md) | pending |
| 5 | 2 | 4 | 3–4 weeks | [Planning tab (slim)](./phase-05-planning-tab.md) | pending |
| 6 | 3 | 5 | 4–6 weeks | [Media, notifications, and growth](./phase-06-media-notifications-growth.md) | pending |

Phases 1–2 are detailed. Phases 3–6 carry scope and acceptance; each gets "Execution Decisions" agreed with the user before its implementation starts, as Phases 4–5 of the previous plan did.

## Dependencies

- Work continues on the PR #4 branch (`fix/finance-refund-split-lock-and-utc-sessions`, user decision 2026-10-06): one branch, one PR. The realignment migration is `000007`, after `000006_merged_guest_authorship`.
- The client (`~/beluno/beluno_app`, KMP) has wired only health and Google sign-in, so contract breaks in Phases 1–2 cost nothing on the client. The OpenAPI compatibility gate needs an explicit accepted-breaks list for them (Phase 1).
- App build order: `~/juntro/plans/261006-1432-beluno-app-ui-build-order/plan.md`. Its stages 1–7 and 9 need Phases 1–3.

## Global acceptance

- Every UI screen in release scope maps to an endpoint or sync entity, or to a documented client-side computation.
- Quality gates of `CLAUDE.md` pass on real PostgreSQL with no mass skips.
- Money rules stay: integer minor units, balanced postings, explainable balances, no silent cross-currency netting, no money movement.

## Unresolved

- FX rate source for "market · est." rates (Phase 2): provider choice; ECB lacks VND.
- Home "Approve 2 splits, 1 receipt scan" implies split approval and receipt OCR; not in the blueprint. Excluded until confirmed.
