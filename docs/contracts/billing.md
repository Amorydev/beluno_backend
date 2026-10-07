# Paid plans: Trip Pass, Pro, and the free limits

## What is sold

| Product | Store type | Unlocks |
|---|---|---|
| Trip Pass | Apple consumable, Google one-time product (consumed by the server) | One trip, for everyone on it, whoever bought it; bound to that trip for good |
| Pro | Apple auto-renewable subscription, Google subscription (yearly) | Every trip its holder owns, and no limit on their own trips |

Hangouts are always free. Prices live in the stores; the product ids the server
accepts come from `BELUNO_STORE_TRIP_PASS_PRODUCT_IDS` and `BELUNO_STORE_PRO_PRODUCT_IDS`
(`GET /v1/me/entitlements` lists them for the app to load from the store).

## Free limits

Both stay off until set (`BELUNO_FREE_ACTIVE_TRIPS`, `BELUNO_MEDIA_RECEIPTS_PER_PLAN`;
assumed 2 and 5).

- **Trips:** a person's own trips (they are the owner) in draft, planning, active, or
  settling, without a Trip Pass. Starting a trip (create or duplicate), reopening a
  completed or cancelled one, restoring one scheduled for deletion (whoever does it:
  the owner's place is taken), or handing a trip in progress to a new owner who has no
  place left answers `409 TRIP_LIMIT_REACHED`. Trips of others never count. Pro removes
  the limit; a trip with a Trip Pass never needs a place.
- **Receipts:** per trip without a Trip Pass or its owner's Pro (`409
  MEDIA_LIMIT_REACHED`). Receipts already there stay when a pass is refunded.

## Buying and restoring

The app sets the buyer's user id on every purchase: Apple `appAccountToken`, Google
`obfuscatedAccountId`. A purchase carrying another account's id is refused. One without
an id (offer codes, promoted purchases, a resubscription from the Play Store) belongs
to the first account that records it. Purchases shared through Apple Family Sharing are
refused: keep Family Sharing off for Pro.

1. Buy in the store.
2. Send it, with `plan_id` for a Trip Pass (never for Pro):
   - `POST /v1/me/purchases/apple` `{signed_transaction, plan_id?}` — StoreKit 2
     `Transaction.jwsRepresentation`;
   - `POST /v1/me/purchases/google` `{product_id, purchase_token, plan_id?}`.
3. Finish the transaction (Apple) once the server answered `200`, or `409` other than
   `PURCHASE_PENDING` (the purchase is recorded or can never be); keep it unfinished on
   `503` and send it again later. The server confirms Google purchases itself (consumes
   a pass, acknowledges Pro); the app must not consume them.

Both routes are safe to repeat: sending a purchase again returns the same `id`, and
`active` and `expires_at` describe the purchase as stored. **Restore** is sending the
current entitlements again (StoreKit `Transaction.currentEntitlements`, Play
`queryPurchasesAsync`); send them at launch too, so a renewal the server missed is
picked up. A Trip Pass needs no restore: it stays on its trip on the server (and a
consumed pass is not in those lists anyway). Read `GET /v1/plans/{id}/entitlement`
before offering a pass, so nobody buys one for a trip already unlocked.

| Answer | Meaning |
|---|---|
| `422 PURCHASE_INVALID` | The store does not vouch for it: forged, another app or environment, unknown product or token |
| `409 PURCHASE_OWNED_ELSEWHERE` | Another account made or recorded this purchase |
| `409 PURCHASE_USED` | This Trip Pass already unlocks another trip |
| `409 PURCHASE_PENDING` | Google: payment pending; send it again once it completes |
| `409 NOT_AVAILABLE_FOR_HANGOUT` | A Trip Pass on a hangout |
| `403 FORBIDDEN` | Guests cannot buy; they create an account first |
| `404 NOT_FOUND` | The trip is unknown or the caller is not in it |
| `503 STORE_UNAVAILABLE` | The store is not configured here or did not answer; retry later |

## Reading entitlements

- `GET /v1/me/entitlements`: `pro` (store and expiry) or null; `active_trips` (own
  trips counting toward the limit) and `active_trip_limit` (null: no limit, or Pro);
  `receipts_per_trip`; the product ids.
- `GET /v1/plans/{plan_id}/entitlement`: `unlocked_by` (`trip_pass`, `pro`, or null),
  `receipts`, and `receipt_limit` (null: no limit). Anyone on the trip may read it.

## Store notifications

- **Apple:** App Store Server Notifications version 2 to
  `POST /v1/store-notifications/apple`. Renewals move Pro's expiry; a refund or
  revocation ends the period it covers (a pass for good), a later paid period starts
  again, and `REFUND_REVERSED` restores it. Payloads not signed through Apple's chain
  answer `422`; notifications about other products are acknowledged and ignored.
- **Google:** real-time developer notifications through a Pub/Sub push subscription to
  `POST /v1/store-notifications/google`, with authentication on (OIDC token, audience
  `BELUNO_GOOGLE_PLAY_PUSH_AUDIENCE`, service account
  `BELUNO_GOOGLE_PLAY_PUSH_SERVICE_ACCOUNT`; Google's signing keys are fetched at most
  every five minutes). The server asks Google for the purchase's state; a voided pass
  and a revoked subscription end at once, a refunded renewal alone does not end a
  subscription, and a subscription replaced by an upgrade or resubscription
  (`linkedPurchaseToken`) stops counting.

Notifications about purchases the server has not seen yet are ignored: the app records
every purchase itself.

## What is kept

Per purchase: the store, product, the store's lasting id (Apple original transaction
id, Google purchase token), the buyer, the trip of a pass, the environment, purchase
time, expiry, revocation, and when Google confirmed it. Signed payloads, prices, and
payment details are not kept. A pass outlives a purged trip as a record of the sale.
