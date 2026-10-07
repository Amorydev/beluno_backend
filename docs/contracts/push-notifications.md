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
| loc args | always `[actor, plan]` for money (actor may be empty), `[plan]` for reminders |
| Android channel | the category (`money`, `reminders`, `summaries`, `news`, `security`) |
| iOS `thread-id` / `category` | the plan ID / the kind |
| data | `notification_id`, `kind`, `category`, and when known `plan_id`, `entity_type`, `entity_id` |

## Kinds

| Kind | Category | Who gets it |
|---|---|---|
| `expense_added`, `expense_edited`, `expense_voided`, `expense_refunded` | money | everyone who paid or shares the expense, except whoever did it |
| `payment_recorded`, `payment_reversed` | money | both sides of the payment, except whoever did it |
| `task_due` | reminders | the assignee, once per due date, from the day before; dropped once the due day is over |
| `poll_closing` | reminders | voters who have not voted, two hours before the deadline; dropped after it |

People merged into another participant are reached as that participant. Reminders
skip plans that are completed, archived, cancelled, or scheduled for deletion, and
only devices whose session is still live are reached. `quiet_start` equal to
`quiet_end` means no quiet hours. `web` tokens get data-only messages for now.

Summaries (daily at 21:00, weekly planning) and news come later.
