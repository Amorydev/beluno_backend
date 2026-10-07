---
phase: 6
title: "Media, notifications, and growth"
status: in-progress
priority: P2
effort: "4–6 weeks"
dependencies: [5]
---

# Phase 6: Media, notifications, and growth

## Overview

Release 3: everything that needs external providers or storage. The previous plan's Phase 7 maps here, cut to the screens.

## Context

- UI screens: Notifications settings (S14), Add memories (S77), Memories (S04), Trip recap (S58), Share recap (S32), Export (S69), Upgrade (S80), Report a problem (S18), Welcome passkey (S16), Me (S49), receipts on Expense detail (S57), cover photo in Trip settings (S10), Search (S27).
- Spec: `trip-os-product-blueprint.md` §9.17–9.20, §12, §16, §18.2, §18.5–18.7.

## Scope

- **Push notifications:**
  - FCM/APNs device tokens; outbox fan-out from activity events
  - categories (money, reminders, summaries, news) with per-user preferences and quiet hours
  - lock-screen previews never show amounts, codes, or addresses
  - nudges for tasks and debts; digests at 21:00 local
- **Media:**
  - S3-compatible storage, signed uploads and downloads
  - receipts on expenses, memories (captions, day/place links, EXIF stripping), album links, trip cover photo
  - malware and type checks, size limits
- **Export:**
  - CSV and JSON (free), PDF trip report and accounting CSV (Trip Pass), with or without receipts
  - "Export my data" for the account
- **Recap and share card:** stats (spent together, per person per day, categories, top place, plans done, polls) and a public card that never includes balances, codes, addresses, or private notes.
- **Monetization:**
  - Free (2 active trips, 5 receipts per trip; hangouts always free), Trip Pass (per trip), Pro (yearly)
  - App Store / Play receipt verification, entitlements, restore purchases
  - "your crew never pays to join, see balances or settle"
- **Passkeys:** WebAuthn sign-in and sign-up next to Google, Apple, and email.
- **Support:** "Report a problem" with category, linked record, and a diagnostic ID that carries no expense text, notes, or codes.
- **Search:** client-side over synced data first; server full-text only if measured need.

## Decisions to confirm before starting

- Push, email, malware-scan, PDF, and storage providers.
- Pricing and limits (blueprint §12 hypotheses).
- FX provider if Phase 2 left it open.

## Execution Decisions (user, 2026-10-07)

- **Order:** slices that need no provider first: (1) export, recap, report a problem; then passkeys, media, push, monetization, PDF reports.
- **Push:** FCM for Android and iOS (APNs through FCM).
- **Malware scanning:** self-hosted ClamAV next to the worker; uploads are scanned before use.
- **Pricing and limits:** the blueprint's hypotheses (Free: 2 active trips, 5 receipts per trip; Trip Pass per trip; Pro yearly; hangouts always free) as settings, changeable without a code change.
- **Exports:** every active participant (viewers and guests too) exports what they can already sync; files download at once and are never stored (the receipt archive becomes a background job with the media slice).
- **Share card:** drawn on the device from public-safe recap fields; the server serves no public link.
- **Report a problem:** stored in the database and read by operators through a script; no email or helpdesk yet.
- The trip-OS blueprint is no longer on this machine; the Stitch screen texts (S04, S14, S18, S27, S32, S58, S69, S77, S80) guide the slices.

## Progress Notes

- Slice 1a (exports) is on `feat/trip-export`:
  - `GET /v1/plans/{id}/export?format=csv|json` and `GET /v1/me/export`, built from the sync snapshot so visibility and secret handling match sync.
  - CSV cells that would read as formulas are kept as text.
  - Audited (`plan.exported`, `account.exported`) and rate-limited (20 per hour per user).
  - Review (`reports/code-reviewer-261007-1530-trip-account-export-review-report.md`): no leak found (export IDs match the sync snapshot). Fixed: files render after the transaction closes, in a worker thread (M1); full-width formula signs and whole-cell escaping (L2); per-route OpenAPI responses (L6); stable CSV order (L7); tests for paging, guests, removed people, raw tokens, private notes, voided rows, and the rate limit.
  - Left as is: an export is read-committed, not a single snapshot (L1); exports go through `scope_access` like sync by design (L3); no size cap yet (revisit with the background export job of the media slice).

- Slice 1b (recap) is on `feat/trip-recap`:
  - `GET /v1/plans/{id}/recap`: days (dates, or local start and end times), stops with nights, people, spending from the budget overview (per person per day, by category in basis points), the most wanted place, itinerary done/total, decided polls, who has settled and when the last settlement was.
  - `share`: route, start date, days, people, total; the app draws the card.
  - Not computed (no data yet): steps, memories, and the cover photo (media slice).
  - Review (`reports/code-reviewer-261007-1600-trip-recap-review-report.md`): money totals matched the budget screen. Fixed: "settled" is the ledger's status and rule (tolerance on the base currency only), and the crew lists people who left with money still open (H1, H2, M1); the top place counts active participants only (M2); `spent_complete` and `estimated_rates` flag partial totals (M3); timed plans return local dates (M4); ending at local midnight ends the day before; category shares add up to 10,000; closed polls without a result are not decisions.
  - Kept, documented: per person per day divides by today's active headcount, placeholders included; `settled_on` is the last standing settlement's date as entered.

## Success Criteria

- [ ] No notification, export, share card, or log carries a secret field.
- [ ] Media uploads work offline-queued, and quota limits follow entitlements.
- [ ] Entitlements are server-verified; the client never decides paid access alone.
