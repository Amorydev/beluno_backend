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

- Slice 1c (report a problem) is on `feat/problem-reports`:
  - Migration `000015_problem_reports`; `POST /v1/support/reports` (10 a day per person) and `GET /v1/support/checks`.
  - Diagnostic code `BLN-XXXX-XXXX` (Crockford base32); the screen's sample `TOS-` prefix was the design project's name.
  - Operators: `scripts/support.py list|show --operator NAME` with the worker role, every read audited (user decision, 2026-10-07: worker role plus audit, not `support_readonly`); account deletion removes reports.
  - Review (`reports/code-reviewer-261007-1630-problem-reports-review-report.md`): definer functions, RLS, record links, and code retry sound. Fixed: merged guests' reports go with the account (H1); control characters refused as 422 (M1); audited operator reads (M2); migration header; voided/reversed records show as deleted; script limit; tests for an exact snapshot key list, insider inserts, strangers, and merged guests.
  - Open: whether the account export should include the person's reports; a retention limit for reports of active accounts (legal policy).

- Passkeys are on `feat/passkeys`:
  - Decisions (user, 2026-10-07): a passkey only signs in (added from Me to an existing account; never creates one); library `webauthn` (py_webauthn).
  - Migration `000016_passkeys`; `/v1/me/passkeys` (options, add, list, rename, remove with recent sign-in) and `/v1/auth/passkey` (options, sign in / step up / guest claim through `authenticate`).
  - Tests drive a software authenticator (`beluno.testkit.passkeys`) with real ES256 keys and WebAuthn bytes.
  - Deploy: `BELUNO_WEBAUTHN_RP_ID`, `BELUNO_WEBAUTHN_ORIGINS`, and the domain's association files (runbook).
  - Security review (`reports/code-reviewer-261007-1700-passkeys-security-review-report.md`): verification and account boundaries sound. Fixed: a sign-in refused after verification (guest merge consent, another account) rolled the used challenge and counter back, letting the response be replayed signed out (H1; the refusal now rolls back alone, guest challenges are bound to the guest); library errors and malformed transports returned 500 (M1, M2); an unusable public key is refused at registration; separate option and sign-in limits, IPv6 counted per /64 (M3); duplicate vs invalid credentials; migration header; owner-only insert/delete and column-limited updates on passkeys; stricter relying-party configuration; `crossOrigin` responses refused; label length capped before cleaning.
  - After a refused guest sign-in the app asks for a new assertion (with `merge_guest_participations: true`); asking for merge consent before the ceremony avoids the second prompt.
  - Left: counter regressions are refused but not audited.

- Media (receipts, covers, album link) is on `feat/media-receipts`:
  - Decisions (user, 2026-10-07): self-hosted RustFS (S3 API; client `boto3`); the server strips all image metadata; the receipt limit is a setting, unset until paid plans; receipts for trips and hangouts, covers and memories for trips.
  - Migration `000017_media`; `/v1/plans/{id}/media` (record, upload URL, uploaded, download URL, delete), sync entity `media`, commands `media.create`/`media.delete`; plan `cover_media_id`, `album_url`.
  - Worker: `media.process` (size, sniffed type, ClamAV INSTREAM, Pillow re-encode, HEIC to JPEG, pixel cap) and `media.delete_objects`.
  - Tests: moto's S3 server and an INSTREAM clamd stand-in (`beluno.testkit.media`) with real signed uploads and downloads.
  - Review (`reports/code-reviewer-261007-1800-media-receipts-review-report.md`): upload binding holds on RustFS, no storage I/O in transactions, purge intact. Fixed: undecodable images got stuck scanning (H1, now rejected `unreadable`; hourly sweep re-queues stuck files); deleting the cover now records the plan change under a plan lock (H2, M5); files deleted or purged mid-scan no longer leave objects behind, and the incoming copy goes only after commit (H3); comments and ICC profiles stripped too (M1); abandoned uploads and reused upload URLs cleaned up (M2); media on its own queue, image work in a thread, worker concurrency 4 (M3); images pinned, scoped keys documented (M4); a definer trigger queues deleted files' objects, the API cannot (M6); tests for the guard, outages, and mid-scan deletes, and the moto limits stated (M7); control characters in album links, clamd size-limit replies, PDFs downloaded as attachments, sniffed-format-only decoding, 503 when storage is missing, per-key deletion failures. The sync `plan` entity now carries the cover and album link.
  - Image limit lowered to 20 MB so clamd's default stream limit scans every file whole.
  - Open (user): account deletion and files others keep; receipts on voided expenses and who may delete a receipt; sanitising PDF receipts.
  - Merged in PR #18.
- Memories are on `feat/memories`:
  - Decisions (user, 2026-10-07): everyone on the trip adds memories (viewers too); organisers pick recap highlights (at most 20); account deletion removes the person's memories and keeps receipts; receipts are also deleted by the expense's creator or `expenses.manage` holders, and never go on voided expenses.
  - Migration `000018_memories` (caption, day, taken time, place, `in_recap`; guard for details, highlights, and receipt deletion; `forget_memories`).
  - `PUT .../media/{id}/memory` (versioned), `PUT .../media/{id}/highlight`; commands `media.update_memory`, `media.set_highlight`; the recap returns the cover, highlights by day and time, and the memory count; the share card the cover.
  - Review (`reports/code-reviewer-261007-1830-memories-review-report.md`): guard and service agree, the highlight limit holds under concurrent picks. Decisions (user): memories and highlights stay open on completed trips and close once archived (new actions `plan.memories.share`, `plan.memories.highlight`); covers stay when their uploader deletes their account. Fixed: guest accounts merged into the actor keep editing and deleting what they posted (service and guard); migration header states it ships with the API build and its locks; uploader index for account deletion; deletes freeze a memory's details; only duplicate IDs report `ALREADY_EXISTS`; tests for plan states, merged guests, covers, and more guard paths.
  - Left: a memory keeps pointing at a place deleted later (like other planning links); the voided-expense check reads the expense without a lock.

- Push notifications (money and reminders) are on `feat/push-notifications`:
  - Decisions (user, 2026-10-07): FCM through `firebase-admin`; messages carry localisation keys the app renders; money and reminders first, summaries and news later.
  - Migration `000019_notifications`: push tokens per session, settings, the outbox, `fan_out` and `queue_reminders` definer functions, a session-revoke trigger.
  - `/v1/me/push-token`, `/v1/me/notification-settings`; `notifications.dispatch` every minute; `docs/contracts/push-notifications.md` lists kinds and keys for the app.
  - Review (`reports/code-reviewer-261007-1930-push-notifications-review-report.md`): privacy and RLS hold. Fixed: tokens cascade with purged sessions, which had broken the auth purge (C1), and only live sessions are reached; every FCM failure maps to a result, batches go through one `send_each` with a 10-second timeout, stuck deliveries respect the attempt cap (H1); dispatch runs never overlap (H2); a commit-ordered queue filled by a trigger replaces the time cursor (M1); fixed loc-arg counts (M2); merged participants are reached (M3); reminders skip finished plans and expire (M4); concurrent token registration serialised (M5); tests for RLS, payments, merged people, devices and sessions, stuck and unconfigured deliveries, plan states, and the purge.
  - Open (user): should a forgiven debt notify the debtor; plan or recipient time zone for reminder days (UTC today).

- Nudges and summaries are on `feat/nudges-summaries`:
  - Migration `000020_nudges_summaries`: `queue_nudge` (API; finds the recipient itself, following merges, and builds the once-a-day key on the recipient's day), `known_zone` (zone names the database lacks fall back to UTC so one profile cannot stop dispatch), `queue_summaries` (21:00 local, trips in progress, others' activity that day), forgiven debts in the fan-out, reminder days in the assignee's time zone.
  - `POST .../tasks/{id}/nudge` (creator or organiser), `POST .../ledger/nudges` (only toward a suggested transfer to the caller); 409 for placeholders, people gone, or oneself, once a day each; `POST /v1/me/sessions/sign-out-others`.
  - Defaults taken without an answer (easy to change): a forgiven debt notifies the debtor; reminder days use the recipient's time zone.
  - Later: weekly planning summary, news.

- Paid plans are on `feat/entitlements`:
  - Decisions (user, 2026-10-07): verify directly with Apple (`app-store-server-library`, Server Notifications v2) and Google (Play Developer API, RTDN over Pub/Sub push), no third party; the free trip limit counts only one's own trips in progress (hangouts free); a Trip Pass unlocks one trip for everyone on it, Pro every trip its holder owns; PDF reports with `fpdf2` (next slice).
  - Migration `000021_entitlements`: `billing.purchases` (own-read RLS; writes through `record_purchase`, `update_purchase`, `acknowledged`), `plan_unlock`, `trips_counting`.
  - `POST /v1/me/purchases/apple|google` (also restore), `GET /v1/me/entitlements`, `GET /v1/plans/{id}/entitlement`, `POST /v1/store-notifications/apple|google`; worker `billing.acknowledge_purchase`; limits in `create`, `duplicate`, reopening, and receipt uploads (`docs/contracts/billing.md`).
  - Decisions after review (user): handing a trip in progress to a new owner needs a place within their limit; a purchase without the buyer's id belongs to the first account that records it, and Family Sharing purchases are refused. Default taken: guests cannot buy.
  - Review (`reports/code-reviewer-261007-2230-entitlements-review-report.md`): store I/O stays outside transactions, RLS holds. Fixed: the limit now also covers restoring, reopening by an admin, and handing over (H1); a refund ends only the period it covers, a later paid period or a reversed refund restores Pro, an app replay never lifts a refund, and answers describe the stored purchase (H2); Family Sharing refused (H3); replaced Google subscriptions stop counting (M1); Google push tokens verified against keys fetched at most every five minutes (M2); misconfiguration, certificate-check outages, token refresh and unreadable answers map to 503, and secure environments require full App Store settings (M3); receipt uploads to one trip count one at a time (M4); notifications for other products ignored (L1); product id checked (L2); expiries bounded (L3); purchases the app consumed itself still confirmed (L5); app guidance on finishing and restoring (L6, L7).

- PDF trip report and accounting CSV are on `feat/pdf-reports`:
  - Decisions (user, 2026-10-07): a money-first report (cover, categories, people, settling up, expenses); receipts marked, not embedded; Noto Sans fonts vendored (OFL).
  - `GET /v1/plans/{id}/export?format=accounting|pdf` behind a Trip Pass or the owner's Pro (`403 UPGRADE_REQUIRED`); the accounting CSV is read from the ledger journal, so it adds up to the balances for every entry kind (tested with refunds, payments, waivers, the kitty, consolidations, corrections, and merges).
  - Review (`reports/code-reviewer-261007-2315-paid-exports-review-report.md`): fixed owner corrections missing from the CSV (C1, now journal-based); the report lists at most 1,000 expenses and renders at most two at a time (H1, user decision); characters the font lacks print as `?` (M1, user decision); the paywall is checked before any data is read and only needed entities load (M2); the misleading rate column is gone and a `person_id` column added (M3, M4); dates of entries without one use the plan's time zone; the merge-depth rule is the ledger's.
  - Defaults taken without an answer: the report is in English; hangouts get the accounting CSV free and no report.

- Decisions for the remaining work (user, 2026-10-07):
  - Market rates from Open Exchange Rates (API key, one call a day), replacing the no-op provider.
  - The PDF report in Vietnamese and English (`lang=vi|en`, defaulting to the person's profile).
  - A weekly planning summary on Sundays at 19:00 local for trips being organised (open tasks, open polls, days to go), and news sent by operators through a script to people with news on.
  - PDF receipts stay as uploaded (scanned by ClamAV) and are only ever served as attachments.
  - A receipt archive for unlocked trips: a worker job builds a zip, kept 24 hours behind a download link; next after the weekly summary and news.
  - Staging on a Singapore VPS with docker compose; a 7-day soak with the Android app.

- The weekly summary and news are on `feat/weekly-news`:
  - Migration `000022_weekly_summary_news`: `queue_weekly` (Sundays from 19:00 local, trips in draft or planning, open tasks and polls and days to go, only when there is something to say) and `queue_news` (operators, per locale, once per key).
  - `scripts/news.py`; news messages carry plain title and body instead of keys.
  - Fixed on the way: `alembic/env.py` let a developer's `.env` redirect migrations away from the database a caller (the test suite) named.
  - Review (`reports/code-reviewer-261008-0000-weekly-summary-news-review-report.md`): local-time logic, dedupe, grants, and message building hold (probed). Fixed: bursts held up other pushes (H1: urgent kinds first, up to ten batches a run, news only to people with a signed-in device); the weekly scan returns at once outside UTC Sunday and Monday and counts once per trip (M1); the news audit keeps what was sent (M2); tests for two time zones, a datetime trip, a poll, and a burst (M3). Left: a weekly summary held by quiet hours past Sunday midnight is dropped.

- The receipt archive is on `feat/receipt-archive`:
  - Migration `000023_receipt_archives`: requests per person (own-read RLS), `archive_entries` for the worker, archive keys in the deletion queue.
  - `POST/GET /v1/plans/{id}/receipt-archives`; worker job `media.build_receipt_archive` writes the zip on disk one receipt at a time, uploads it, and queues its deletion a day later; `receipts.csv` lists each file's expense.
  - Review (`reports/code-reviewer-261008-0040-receipt-archive-review-report.md`): fixed the security sweeps missing the table and route (C1); builds a crash cut short failed by the hourly sweep (H1); zips built on a disk volume, one build at a time (H2); voided expenses left out, a default taken without an answer (H3); ready archives reused until new receipts arrive, sizes checked before reading, 5 requests an hour (H4); one archive in the making per person (M1); a purged plan's zips deleted at once (M2); tests for each. Left: a receipt deleted after a zip was built stays in that zip for its day.

## Success Criteria

- [ ] No notification, export, share card, or log carries a secret field.
- [ ] Media uploads work offline-queued, and quota limits follow entitlements.
- [ ] Entitlements are server-verified; the client never decides paid access alone.
