# Media receipts: code review (uncommitted, branch feat/media-receipts)

## Scope
- Migration 000017, storage.py, malware.py, media_files.py, modules/media.py, plans cover/album, API router/commands/projection, worker tasks, config, compose/env, tests, docs.
- How I verified: read the code. Ran live probes in the session scratchpad (Testcontainers PG + moto + fake clamd) and against a throwaway `rustfs/rustfs:latest` container (built 2026-10-03, since removed). Fuzzed `clean()`. Ran the repo gates.
- Gates: ruff, ruff format, and mypy strict all pass. The 33 targeted tests pass (test_media, test_media_files, test_config, test_plan_purge, idor sweep, rls isolation). `openapi.json` matches the export.

## Overall
The basic structure is sound. Bytes never pass through the API. The served copy is always made from the one read that was scanned. Only the worker can mark a file ready. The purge function is intact. The problems are in failure paths: malformed files leave a file stuck in `scanning`, a race leaves a deleted file's object in storage, and deleting the cover sends no plan change to sync. The metadata strip also lets a JPEG comment and the PNG ICC profile through. The test harness also claims a check it does not make.

## Critical
None.

## High

**H1. Malformed images crash the job, and the file stays `scanning` forever** (verified live)
- `media_files.py:76-84` only catches `UnidentifiedImageError, DecompressionBombError, OSError, ValueError`. Fuzzing found `SyntaxError` (bad EXIF TIFF header in WebP/HEIC, broken PNG chunk), `EOFError` and `RuntimeError` (pillow_heif), and `struct.error` (JPEG). All of them escape.
- Live run: a WebP with corrupt EXIF made `process_media_upload` raise `SyntaxError`. The row stayed `scanning` and `incoming/{id}` stayed in storage.
- The job then retries with `RetryStrategy(max_attempts=8, exponential_wait=5)` (`worker/tasks.py:166`), about 5.6 days in total. Every attempt downloads and scans the file again. After the last attempt nothing settles the row: `mark_uploaded` returns early on `SCANNING`, and no job sweeps stuck files.
- Any contributor or guest can trigger this cheaply. A phone photo with a damaged EXIF block triggers it too.
- Fix:
  - In `_reencode`, wrap the whole decode, transpose, and encode in `except Exception` and raise `RejectedFile("unreadable")`.
  - In `process_media`, retry only on `ScannerUnavailable`, `ClientError`, and `BotoCoreError`; turn any other exception into `rejected/unreadable`.
  - Add a sweep that settles rows still `scanning` after N hours.

**H2. Deleting the current cover changes the plan but writes no sync change; a race can also reuse a plan version** (no change row verified live)
- `modules/media.py:165-170` sets `plan.cover_media_id = None` and bumps `plan.version` by hand. It never calls `record_plan_change`.
- Live run: the plan went from v2 to v3, and `change_log` has plan rows only for v1 and v2. Devices pulling the plan scope never learn the cover was cleared, and keep pointing at a deleted media.
- `load_plan` there runs without `for_update`. If an `update_plan` commits between the read and the write, both commits produce the same version number (v N+1 twice). The sync contract assumes one change per version.
- Fix: call `load_plan(ctx, plan_id, for_update=True)` (plan row first, then the media row), and call `record_plan_change(ctx, plan, "plan.updated")`.

**H3. A file deleted or purged while the worker processes it leaves its cleaned copy in storage forever** (verified live)
- `modules/media.py:239-247` writes `media/{id}` and deletes `incoming/{id}` before the final transaction.
- Live run: the file was deleted mid-scan and `media.delete_objects` ran (2 keys). The worker then wrote `media/{id}` and returned. The object stayed in storage and the deletion queue was empty.
- Purge path: the purge removes the row, so `scalar_one()` at `media.py:245` raises `NoResultFound`. The job retries, the first transaction sees no row ("missing"), and `media/{id}` is orphaned.
- If the final transaction fails for any other reason, the retry finds `incoming/` already gone and marks the file `rejected/missing`, while `media/{id}` (never queued) stays.
- This breaks the retention-matrix promise for receipts the user asked to delete.
- Fix:
  - In the final transaction, use `scalar_one_or_none()`. If the row is gone, deleted, or no longer scanning, insert `ObjectDeletion(ready_key)` (the worker role already has INSERT).
  - Delete `incoming/` only after that transaction commits.

## Medium

**M1. "Strip all image metadata" (a user decision) is not fully met** (verified)
- Pillow 12 copies `im.info["comment"]` into the JPEG it saves: a JPEG COM segment survives re-encoding (`comment leaked: True`).
- PNG output keeps `icc_profile` from `im.info`. ICC profiles can carry device make and model.
- So the comment at `media_files.py:99` ("Nothing is copied over") is false.
- `tests/unit/test_media_files.py:27` only checks EXIF.
- Fix: before saving, call `image.info.clear()` (or copy the pixels into a fresh `Image.new`). Then test for comment, ICC, XMP, and PNG text chunks.
- Side effect: iPhone Display-P3 HEIC files lose their profile, so colours shift. If that matters, convert to sRGB with ImageCms rather than keeping the ICC.

**M2. Unbounded orphaned uploads, and the retention doc overstates cleanup** (re-upload verified live)
- `awaiting_upload` rows never expire, and nothing cleans up `incoming/` objects whose upload was never reported.
- A presigned PUT stays valid for 15 minutes after the file settles. Live run: a second PUT after the file was `ready` returned 200 and left `incoming/{id}` in storage until that media is deleted.
- The receipt quota is unset (a user decision), and up to 300 upload URLs can be signed per 10 minutes per user. One guest can fill the self-hosted disk.
- `retention-matrix.md:20` says "the unscanned upload is removed once scanned", which is not true for these paths.
- Fix (quotas stay unset):
  - A periodic job that rejects (`missing`) and queues keys for `awaiting_upload` rows older than about 24 hours.
  - A RustFS lifecycle rule that expires `incoming/` after 1–2 days.
  - Check the worker deletes `incoming/` after the final commit (H3).
  - A per-user rate limit on `media.create`.

**M3. Media processing can starve sign-in emails, and image work blocks the worker's event loop**
- `MEDIA_QUEUE = "maintenance"` (`media.py:49`). The worker runs `run_worker_async` with procrastinate's default `concurrency=1` over `["maintenance", EMAIL_QUEUE]`.
- `clean()` (Pillow decode of up to 60 MP, `PNG optimize=True`, HEIC decode) runs synchronously on the event loop, using hundreds of MB and seconds of CPU per file.
- A burst of uploads from any contributor or guest delays OTP and magic-link delivery and every maintenance job. The H1 retries make it worse.
- Fix: run `clean` with `anyio.to_thread.run_sync`, and give media its own queue and worker process (or raised concurrency). Consider `Image.draft()` for JPEG.

**M4. Compose: RustFS root credentials are the app keys, and the RustFS image is unpinned**
- `compose.yaml:203-204` and `env.example:34`: the API and worker hold RustFS root keys, so an API compromise gives full admin of storage (all buckets and policies).
- Presigned URLs carry whatever rights the signing key has.
- `rustfs/rustfs:${RUSTFS_VERSION:-latest}` (`compose.yaml:197`) is unpinned. RustFS is young software, and I verified that signed Content-Length and Content-Type are enforced only on the 2026-10-03 build.
- Fix: separate keys with bucket policies:
  - API signing key: PutObject on `incoming/*`, GetObject on `media/*`.
  - Worker key: Get/Put/Delete, plus CreateBucket or pre-create the bucket.
  - Pin RustFS (and ClamAV) by version or digest, and keep root keys only on the RustFS service.

**M5. Race between setting a cover and deleting it**
- `ready_cover` (`media.py:187-199`) reads the media row without a lock.
- `update_plan` locks the plan; `delete_media` locks only the media row.
- Interleaving: an `update_plan` that sets the cover runs alongside a `delete_media` of that file. Both commit, and `plans.cover_media_id` now points at a deleted row (the foreign key is still satisfied).
- Fix: `ready_cover` takes `SELECT ... FOR SHARE`, and `delete_media` locks the plan first (same order as H2).

**M6. `object_deletions` lets a guarded insider delete any plan's bytes**
- `000017:186-190` grants `api_runtime` INSERT with `WITH CHECK (true)`.
- `guard_media` only lets the uploader or an organiser delete a row. But anyone running SQL as `api_runtime` (the threat model the write-guards address) can queue `media/{any id}` and wipe another plan's files.
- Fix: queue the keys from a SECURITY DEFINER AFTER UPDATE trigger when `deleted_at` goes from NULL to set, and revoke INSERT from `api_runtime`. Or add a check that the key's media row is deleted and the actor manages it.

**M7. The tests do not prove the upload binding, and some are phantom**
- `testkit/media.py:3-4` claims moto "verifies SigV4 signatures". Probe: moto accepted a tampered signature (200) and a body of the wrong size and type (200).
- So no repo test proves that a PUT different from the declaration is refused.
- I checked RustFS by hand: the presigned PUT signs `content-length;content-type;host`, and a bigger body, a wrong type, or a chunked body with no length all get 403.
- Other gaps:
  - No DB-level test of `guard_media` (as insider: API sets `ready`, someone other than the uploader moves a file to scanning, the worker deletes a file).
  - No scanner-down or retry test.
  - No sync test of the plan after the cover is deleted (H2).
  - `test_media.py:215`, `{"kind":"cover","content_type":"application/pdf"}` by Bea, returns 403 because of her role, so it never reaches the "PDF only for receipts" check.
- Fix:
  - Correct the docstring.
  - Add an opt-in RustFS or MinIO contract test, or assert on `X-Amz-SignedHeaders`.
  - Add the guard tests, an `ObjectStorage` read-failure retry test, and the plan-sync assertion; send the PDF-cover case as the owner.

## Low
- **L1. Bad characters in `album_url`** (`contracts/plans.py:46`):
  - The pattern `[^\s]+` lets NUL through, giving a live 500 (`psycopg.DataError: ... NUL`).
  - Bidi and control characters are accepted too.
  - Add `AfterValidator(reject_control_characters)`; consider requiring a host (`urlparse`) and refusing bidi characters.
- **L2. clamd limits:**
  - `INSTREAM size limit exceeded. ERROR` maps to `ScannerUnavailable`, so the job retries for days instead of rejecting the file as `size`.
  - clamd's `StreamMaxLength`/`MaxFileSize` must stay at least `media_*_max_bytes`. A file over `MaxFileSize` can come back as `OK` without being scanned unless `AlertExceedsMax` is set.
  - Document this coupling, or set the limits in a mounted `clamd.conf`.
- **L3. PDFs are stored as uploaded:**
  - JavaScript, embedded files, and author metadata all stay.
  - They are served `inline` from the storage origin, with no `X-Content-Type-Options: nosniff` (RustFS does not send it).
  - The risk is low (PDF viewers isolate the document; the Content-Type is forced). Either use `attachment` for PDFs or record this as an accepted risk.
- **L4. Formats:** `Image.open` is not restricted to the sniffed format; pass `formats=[...]` as defence in depth.
- **L5. Receipt rules:**
  - Receipts can be attached to voided expenses.
  - Holders of the MANAGE_EXPENSES capability and the expense's creator cannot delete someone else's receipt.
  - `delete_media` checks only VIEW, so deletes work in any plan state.
  - These are product decisions, not defects; confirm them.
- **L6. Errors when storage is unset:** `StorageNotConfigured` (dev only, storage not set) returns 500, not the documented 503.
- **L7. Deletion worker:**
  - `delete_queued_objects` handles 200 keys every 10 minutes; a large purge is slow to clear.
  - The batch keeps the same oldest-first order, so one key that keeps failing blocks the whole queue.
  - Download URLs already issued keep working for up to 5 minutes after deletion (accepted).
- **L8. Guarded-insider gaps:** the INSERT guard does not check the role for `kind` (a viewer, as insider, can insert a `cover` or `memory` row) and does not pin `size_bytes`/`width`/`height`. Low, because only the worker can make a row `ready`.

## Verified correct
- **Upload binding:**
  - Content-Length and Content-Type are signed into the presigned PUT, and RustFS enforces them.
  - The worker also checks `len == declared_size`, reads at most `max+1` bytes, and sniffs the type.
- **Scan-then-upload race:**
  - The stored copy is made from the single read that was scanned, so a second PUT cannot change what is served.
  - Uploads only ever reach `incoming/`, and only the worker writes `media/`.
- **Access:**
  - `upload-url` is limited to the uploader while the file awaits upload.
  - Downloads require `ready`: a deleted file returns 404, a rejected one 409, and a stranger gets 404.
  - Every lookup filters on `plan_id`.
  - Worker transitions are limited to settling `scanning` files.
- **Purge function:** compared with 000014's version, the media block is the only change; nothing was dropped. Cover is cleared before the media rows, which go before the expenses.
- **No storage I/O inside a database transaction** in `process_media` or `delete_queued_objects`. Presigning is local.
- **Metadata:** EXIF, GPS, and XMP are stripped from JPEG, PNG, WebP, and HEIC→JPEG. Animated images become one frame. The pixel cap is checked before decoding.
- **Sync and export:**
  - The `media` sync entity carries no URLs.
  - The export includes media rows through the generic projection.
- **Config:** settings fail closed (storage is required for api, worker, and all; the scanner for worker and all; the public URL must be https).

## Recommended actions (order)
1. H1: catch all decode errors and retry only on infrastructure errors.
2. H3: queue `media/{id}` for deletion when the row is gone or deleted; delete `incoming/` after commit.
3. H2 and M5: lock the plan, record the plan change on cover delete, and take FOR SHARE in `ready_cover`.
4. M1: clear `image.info` before saving and extend the unit test.
5. M2: add the expiry job for files never uploaded and a lifecycle rule on `incoming/`.
6. M3: run `clean` in a thread and give media its own queue or worker.
7. M4: scoped keys and pinned images. M6: trigger-based deletion queue.
8. M7 and L1–L2: fix the tests, the album URL validator, and the clamd limit handling.

## Unresolved questions
- Should a user who deletes their account also lose the receipts and covers they uploaded to plans that others keep? Today those files stay with the plan, like expenses do. The retention doc does not say either way.
- Is a dedicated media worker process acceptable on staging (memory budget beside ClamAV's ~2 GB)?
- Should receipt PDFs be flattened or rasterised, given the user's "strip all metadata" decision covered images only?
