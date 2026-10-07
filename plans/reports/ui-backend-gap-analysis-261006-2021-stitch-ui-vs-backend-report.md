# UI vs backend gap analysis (Stitch "Trip OS" UI)

Date: 2026-10-06. Sources:

- Stitch project `4246966303110169271` ("Design System Implementation"): 86 screens, 77 UI screens plus DESIGN.md v3 "Midnight Wallet" (several screens are style variants of one function).
- Product spec the UI was built from: `~/juntro/docs/trip-os-product-blueprint.md` (v1.1, 3,154 lines), UI plan `~/juntro/plans/261005-1906-trip-os-stitch-ui-design/`, app build order `~/juntro/plans/261006-1432-beluno-app-ui-build-order/plan.md`.
- Client: `~/beluno/beluno_app` (Kotlin Multiplatform; design system and navigation shell only; only health and Google sign-in are wired).
- Backend: this repo at `main` plus PR #4.

## Verdict

**Realign the backend, do not rebuild.** About 60% of the backend effort sits in platform, identity, offline sync, and the finance ledger. The UI needs all of that almost as built, and it is the hardest part to get right. The mismatch is at the top of the domain model and in features not built yet:

1. **Shape.** The UI is trip-first: `trip` and a light `hangout`, plus `crews` as reusable people lists. The backend generalised it into durable `Group` containers (roles, group invites, group-visible plans, a "reader" level) with generic `Plan`s, recurring series, and a travel extension. The UI uses none of the group, series, or travel concepts.
2. **Money details.** The ledger core matches, but a handful of UI behaviours are missing: settle everything in the base currency at a frozen rate set, ledger confirmation by members, a "settled under" tolerance, a changeable base currency, expense time and time zone, personal expenses, default member shares, kitty targets and cash counts, and per-trip permission toggles.
3. **Not built yet.** Planning tab (itinerary, places, polls, bookings), tasks and packing, activity feed, notifications, people and crews views, media (receipts, memories), export, recap, monetization, support, passkeys, and account deletion.

The money really is Splitwise-like: the app never moves money. Settlements are recorded ("Trip OS doesn't move money"), and the kitty is "a shared record. It doesn't hold or move real money."

## Alignment by area

Legend: ✅ keep as is · 🔧 adjust · ➖ unused by the UI · ❌ missing.

| Area | What the UI needs | Backend today | Verdict |
|---|---|---|---|
| Platform | FastAPI, problem+json, RLS, jobs, rate limits | done | ✅ |
| Sign-in | email link, Apple, Google, **passkey**; sessions; sign out other devices | email OTP/magic link, Apple, Google, sessions, device revoke | 🔧 add passkeys (WebAuthn) |
| Guest and claim | join as guest with a name; "Keep your trips"; merge into an existing account | guests via invites, claim and merge flows, merged-guest authorship (PR #4) | ✅ |
| Account deletion | delete account, the person becomes "Former member" | none | ❌ |
| Trip container | `trip` / `hangout`, destinations (multi-city), dates, cover, base currency, status, archive, duplicate (members and settings), delete with grace | generic `Plan` (`kind`, timing, states, visibility, deletion grace, duplication) | 🔧 add type, destinations, cover, settings; drop `visibility` and `group_id` usage |
| Members | owner/admin/member/viewer + guest badge; join approval; former members kept; transfer ownership; **default share per member**; headcount ("6 of 8 slots") | participants, roles, states, join requests, placeholders, transfer, remove | 🔧 add `default_share_weight`, expected headcount |
| Per-trip permissions | "Edit others' expenses: Admins", "Manage budget: Admins + Quân" | fixed role policy | ❌ trip settings plus per-member grants |
| Invites | link with expiry, approval on/off, turn off, open slots | plan invites with approval, expiry, revoke | ✅ |
| Groups | none; **Crews** are saved people lists ("Brings the people only. Balances stay in each group") | durable groups with roles, group invites, group-visible plans, reader access, group sync scope | ➖ replace with lightweight crews |
| Recurring plans | none | plan series, recurrence, horizon job | ➖ |
| Travel extension | none (destinations and bookings cover it) | travel details and segments | ➖ superseded |
| RSVP | hangout "All checked in 5/5"; itinerary attendance | plan RSVP | ✅ reuse as check-in |
| Offline sync | "Saved on this device", sync queue, conflict review ("Keep mine / Keep Minh's") | command catalog, idempotency, cursor pull, 412 with snapshot, tombstones | ✅ |
| Expenses | multi-payer, equal/exact/%/shares/items/**adjustment**, refund, void, history log, linked plan item and booking, **paid time + zone**, receipts, **personal** expense | revisions, multi-payer, equal/exact/percentage/shares/itemized, refund, void, date only, commitment link only | 🔧 add `occurred_at` + zone, personal flag, item/booking link, adjustment split (or client-side exact), readable history |
| FX | "market · est." rates, "Use my card's rate", rate saved with the expense | manual/estimated/agreed snapshots, no rate feed | 🔧 add a server rate feed for clients to cache |
| Base currency | changeable; "re-estimates totals; original amounts stay" | locked once finance data exists | 🔧 allow a change and re-estimate base snapshots |
| Balances | net per person per currency, "All in VND" view, "Why you owe Minh", adds up to 0 | per-currency balances, explanation per account, journal | ✅ (+ base view) |
| Settle up | per currency **or all in base at frozen rates**, "Ledger confirmed by 6 of 6", "Settled under 1,000 ₫", partial payments, channel, reverse | per-currency preview, record (with conversion), confirm/dispute/reverse, waiver | 🔧 settlement plan with frozen rates, member ledger confirmation, tolerance |
| Budget | total, categories (custom?), per person, daily pace, actual/committed/planned, over budget, "count personal expenses" | total/category/participant/daily budgets, commitments with tiers | 🔧 personal toggle; confirm whether categories are custom |
| Kitty | holder, per-member target ("¥5,000 of ¥10,000"), paid-from-kitty, **cash count** by the holder | fund with custodian, contributions, withdrawals, fund-paid expenses; adjustment is owner + step-up | 🔧 targets, a custodian count adjustment |
| Itinerary | days D1..Dn, items with time, duration, place, lead, attendance, estimate vs spent, booking code, done, anytime items | none | ❌ (slimmer than the old Phase 6) |
| Places | paste a Maps link ("details load later"), want-to-go votes, notes, status (shortlist, poll winner, in plan), add to plan | none | ❌ |
| Polls | single choice and yes/no with quorum ("needs 4"), deadline, decided/locked, actions (save place, add to plan, assign booking) | none | ❌ (no availability or multi-choice in the UI) |
| Bookings | types, masked code with Reveal, check-in/out with zone, travelers, price, payment status, free-cancellation deadline, "counted as committed", "Create expense from booking" | commitment port only | ❌ |
| Tasks | Mine/All/Done, assignee, due, status, reminder, **nudge** | none | ❌ |
| Packing | shared vs private ("Only you can see this"), categories, owner, packed count, move to shared | none | ❌ |
| Activity | cross-group feed with readable diffs ("Equal → Shares", "90M → 96M") | audit events hold IDs and versions only | ❌ member-facing activity model |
| Notifications | categories, quiet hours, digests, lock-screen privacy, nudges | none | ❌ |
| People and crews | per-person balances across groups (per group, never netted), crews, start a hangout or trip from a crew | none (groups are not crews) | ❌ |
| Media | receipt photos (Free limit 5/trip), memories, EXIF strip, album link | none | ❌ |
| Export, recap, share card | CSV/PDF/JSON with receipts; recap stats; public card with privacy rules | none | ❌ |
| Monetization | Free (2 active trips), Trip Pass $6.99, Pro $29.99/yr, restore purchases | none | ❌ entitlements and store receipt checks |
| Support | report a problem with a diagnostic ID | none | ❌ |
| Invitee web | guest web view: balance, vote, today, tasks, quick expense | API supports guests | ✅ backend (web client is separate) |

## Unused by the UI (candidates to remove or freeze)

- Groups as containers: group roles, group invites, group-visible plans, `reader` sync level, group sync scope (`modules/groups/*` ~670 lines plus policy, RLS, and sync wiring).
- Plan series and recurrence plus the horizon job (`series.py`, `recurrence.py` ~640 lines).
- Travel details and segments (`travel.py` ~240 lines).
- Phase 6 items the UI does not show: availability and multi-choice polls, a typed link graph, server reorder and rebalance APIs beyond itinerary order, and the network place resolver (the UI accepts "details load later").

The repo is pre-launch and the app has not integrated these endpoints, so removing them now is cheap; later it is a contract break.

## Proposed roadmap (replaces Phases 6–8)

| Step | Scope | Notes |
|---|---|---|
| R1 Realign core | trip/hangout type, destinations, cover, settings (settle tolerance, permission toggles), member default share and headcount; crews replace groups; remove series and travel; drop group visibility and readers | migration `000007`; API naming stays `plan` (a hangout is a plan) unless decided otherwise |
| R2 Money gaps | `occurred_at` + zone, personal expense, default shares, item/booking links, base-currency change with re-estimation, server FX feed, settlement plan in base currency at frozen rates, ledger confirmation, kitty targets and counts, readable expense history | finance stays the money owner |
| R3 Planning (slim) | itinerary, places (manual + offline link parsing), polls (single, yes/no + quorum, deadline job), bookings (encrypted code, reveal), tasks, packing (private items in the owner's user scope) | follows the UI screens only |
| R4 Social and lifecycle | activity feed, notifications and preferences, people and crews views, media (S3), export, recap and share card, monetization, support, account deletion, passkeys | app build order phases 9–11 |
| R5 Release | the old Phase 8 gates, sized to the new scope | |

## Decisions needed

1. Realign (recommended) vs rebuild.
2. Remove groups, series, and travel now, or freeze them (keep the code, stop extending it).
3. API naming: keep `plan`, or rename to `trip` before the app integrates.
4. Settlement: add "all in base currency at frozen rates" next to per currency (the UI shows both).
5. Custom budget categories ("Add category") or the fixed category list.
6. Release scope: everything in the UI at once (as the blueprint says), or a first release (trip + hangout + money + offline + invites + people) followed by planning and media.

## Unresolved questions

- Is the Stitch canvas final, or are the style variants (Tactile / Editorial / Architectural) still being chosen? The backend is unaffected either way.
- Home "Approve 2 splits, 1 receipt scan" implies split approval and receipt OCR, which no other screen or the blueprint describes. Confirm before building.
