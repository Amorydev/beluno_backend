# Push notifications

The server sends FCM messages (Android, and iOS through APNs) that carry
localisation keys, never finished text. The app ships the strings for every key in
every language; the lock screen never shows amounts, booking codes, or addresses.

## Registering a device

- `PUT /v1/me/push-token` `{token, platform: ios|android|web}` binds the FCM token to
  the current session. A token registered by another session moves here. Signing
  out, revoking the session remotely, or deleting the account removes it.
- `DELETE /v1/me/push-token` stops notifications to this device.

## Settings

`GET`/`PUT /v1/me/notification-settings` (`If-Match: "<version>"`, `"0"` before
the first save): `money`, `reminders`, `summaries`, `news` (off by default),
`quiet_hours` with `quiet_start`/`quiet_end` in the person's own time zone (profile
`timezone`, UTC when unset; default 22:00-07:00). Notifications that arrive during
quiet hours wait until they end. Security notifications ignore both.

## Message shape

| Field | Value |
|---|---|
| Android `title_loc_key` / `body_loc_key` | `notification_<kind>_title` / `notification_<kind>_body` |
| iOS `title-loc-key` / `loc-key` | the same keys |
| loc args | always `[actor, plan]` for money and nudges (actor may be empty), `[plan]` for other reminders, `[plan, count]` for the daily summary, `[plan, open tasks, open polls, days to go]` for the weekly one (days empty when the trip has no date) |
| News | no keys: `title` and `body` carry the team's own words, in Vietnamese or English by the profile's locale |
| Android channel | the category (`money`, `reminders`, `summaries`, `news`, `security`) |
| iOS `thread-id` / `category` | the plan ID / the kind |
| data | `notification_id`, `kind`, `category`, and when known `plan_id`, `entity_type`, `entity_id` |

## Kinds

| Kind | Category | Who gets it |
|---|---|---|
| `expense_added`, `expense_edited`, `expense_voided`, `expense_refunded` | money | everyone who paid or shares the expense, except whoever did it |
| `payment_recorded`, `payment_reversed`, `waiver_given` | money | both sides of the payment or forgiven debt, except whoever did it |
| `task_due` | reminders | the assignee, once per due date, from the day before; dropped once the due day is over |
| `poll_closing` | reminders | voters who have not voted, two hours before the deadline; dropped after it |
| `task_nudge` | reminders | an open task's assignee, when whoever added it or an organiser nudges (`POST .../tasks/{id}/nudge`; once a day per task and assignee) |
| `payment_nudge` | reminders | someone the ledger's suggested transfers say should pay you, when you nudge them (`POST .../ledger/nudges`; once a day per pair) |
| `daily_summary` | summaries | everyone on a trip in progress, at 21:00 their time, when others did something there that day |
| `weekly_summary` | summaries | everyone on a trip being organised (draft or planning), Sundays from 19:00 their time, when it has open tasks, open polls, or a start date ahead |
| `news` | news | people who turned news on and have a signed-in device, when the team sends news (`scripts/news.py`; once per piece of news) |

People merged into another participant are reached as that participant. Reminders
skip plans that are completed, archived, cancelled, or scheduled for deletion, and
only devices whose session is still live are reached. `quiet_start` equal to
`quiet_end` means no quiet hours. `web` tokens get data-only messages for now.
Delivery takes money, reminders, and security first, then summaries, then news, up to
1,000 a minute, so a burst never holds up a payment notification.

"Today" for task reminders, nudges, and the summary is the recipient's own day
(profile time zone; UTC when unset or when the database does not know the name). A
nudge answers `{"queued": false}` when it was already sent that day, and `409` when
nobody can receive it: a name-only placeholder, someone who left or was removed, a
deleted account, or the nudger themself. Task nudges work while the plan is being
organised or settled; payment nudges also after it is completed.
