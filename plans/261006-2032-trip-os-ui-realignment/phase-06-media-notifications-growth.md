---
phase: 6
title: "Media, notifications, and growth"
status: pending
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

## Success Criteria

- [ ] No notification, export, share card, or log carries a secret field.
- [ ] Media uploads work offline-queued, and quota limits follow entitlements.
- [ ] Entitlements are server-verified; the client never decides paid access alone.
