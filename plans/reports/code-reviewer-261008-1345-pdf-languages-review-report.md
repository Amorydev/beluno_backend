# Review: PDF trip report in Vietnamese and English (`feat/pdf-languages`, uncommitted)

Scope: `src/beluno/api/report_text.py` (new), `trip_report.py`, `paid_exports.py`, `routers/exports.py`, `tests/unit/test_report_text.py`, `tests/integration/test_paid_exports.py`, README, `docs/contracts/billing.md`, `openapi/openapi.json`, phase-06 note.

## Gates (run by reviewer)

- `ruff check .` clean; `ruff format --check .` clean; `mypy src` (strict) clean.
- `tests/unit/test_report_text.py`: 2 passed.
- `tests/integration/test_paid_exports.py` against live PG: 6 passed, 0 skipped.
- OpenAPI: regenerated in memory == committed snapshot; `check_openapi_compatibility.py main -> current` exit 0 (optional query param, additive).
- `fpdf2` 2.8.9 `FPDF.set_lang(lang: str)` exists (sets catalog `/Lang`, bumps min PDF version to 1.4). `"vi"`/`"en"` are valid RFC 3066 tags. OK.
- Rendered sample vi/en reports to PNG (scratchpad): layout fits; `Hoá đơn` header fits the widened receipt column; `12 Mar 2027` fits the date column; huge VND amounts wrap cleanly.

## Critical

None.

## High

None.

## Medium

**M1. Deleted accounts print as English "Former member" in the Vietnamese PDF.** `paid_exports.py:101-102,119-120`, `modules/account_deletion.py:49,144`.
Account deletion writes the literal `display_name = "Former member"` to every participant row in the DB. `names_of` returns it as a person's own text, so the vi report shows "Former member" for deleted people (People table, settle-up From/To, Paid by), while the code-level fallback `who()` shows "Cựu thành viên". The `who()` fallback almost never fires (left participants still sync), so in practice the localized term is mostly dead and the English one is what Vietnamese readers see.
Fix: in `plan_pdf`, map names equal to `account_deletion.FORMER_MEMBER` to `report_text.text("former_member", language)` (a name collision with a person really called "Former member" is negligible), or better, key on a deletion marker if the participant projection exposes one. Add an assertion in the vi integration test using `exercise_money_and_members` or an account deletion.

## Low

**L1. `%b` depends on the process LC_TIME.** `report_text.py:105-106`.
Probe: after `locale.setlocale(LC_TIME, "fr_FR.UTF-8")`, `day(date(2027,3,12),"en")` -> `12 mars 2027`; `de_DE` -> `12 März 2027`. Today nothing in `src/` or installed runtime packages calls `setlocale` (only pip), and CPython sets only LC_CTYPE at startup, so English month names are correct now. It is a latent, process-global hazard (any future dependency or `setlocale(LC_ALL, "")` silently changes English reports). Fix: a fixed `("Jan", ..., "Dec")` tuple: `f"{value.day:02d} {MONTHS[value.month - 1]} {value.year}"`. Cheap and removes the dependency.

**L2. Vietnamese spelling/terminology consistency** (`report_text.py`):
- `Tỉ lệ` (line 26) vs `tỷ giá` (lines 56-60): mixed i/y spelling of the same syllable in one document. Pick one; `Tỷ lệ` matches `tỷ giá` and is the more common form in apps.
- `Hoá đơn` (line 46): old-style tone placement; `Hóa đơn` is what most current VN apps/banks show. Either is correct; choose one convention for all future strings.
- `Phần chịu` (line 31): understandable, slightly stiff. `Phần chia` or `Phải chịu` reads more naturally for "share of expenses". Acceptable as is.
- `Thanh toán` (line 37) as "To settle up": acceptable; `Cần thanh toán` or `Ai trả ai` is clearer (bare `Thanh toán` also reads as "payment/checkout").
- `Quy đổi` for Base column: sensible. `Quỹ chung` for kitty: good, natural. `Cựu thành viên`: correct but formal; `Thành viên cũ` / `Người đã rời` is more conversational. Either is fine.
- `Người` (line 28) as a column header is bare; `Tên` or `Thành viên` reads better.
- `tiền cơ sở` (line 57): non-standard; `tiền tệ gốc của chuyến` or `tiền tệ chính` is clearer to lay users.
- `Hoạt động` for activities: fine; `Vui chơi` / `Tham quan` is more trip-natural.
None of these are wrong; the i/y mix is the only real inconsistency.

**L3. English report output changed.** `paid_exports.py:165-167,183`: expense dates were ISO `2026-10-06`, now `06 Oct 2026`; footer was ISO, now `12 Mar 2027`. Probably intended (matches cover dates), but it is a visible change to the English report not mentioned in docs/PR. Confirm.

**L4. `lang` is accepted for every format and silently ignored outside PDF.** `routers/exports.py:43-45`. `format=csv&lang=fr` now returns 422; `format=csv&lang=vi` returns an untranslated CSV. No data leak; the accounting CSV is unchanged and still uses `FUND`/"Former member" (verified diff: `plan_accounting_csv` untouched). Description already says "The PDF's language". Acceptable; optionally note "ignored for other formats" in the description.

**L5. Test gaps** (tests are real, no vacuous asserts):
- No check that every `TEXT` entry has both languages and identical `{placeholders}`; a typo in a rarely-hit key (`unconverted`, `estimated`, `more_expenses`, `dates_not_set`, `former_member`) is a 500 only in that branch. Add a unit test iterating `TEXT`/`CATEGORIES`. (Reviewer verified placeholders match today.)
- No vi negative/zero money case (`money(-123456,"USD",…,"vi") == "-1.234,56 USD"`; verified by probe, not by tests).
- No test that explicit `lang=en` overrides a vi profile; no vi assertion for kitty (`Quỹ chung`) or former member (would catch M1).
- `pdf_text` helper is added mid-file while `test_the_trip_report_shows_the_money` still inlines the same extraction; reuse it there.

## Verified OK (risk calibration)

- Money edge cases (probe): negative (`-1.234,56 USD`), zero (`0,00 USD`, `0 VND`), 0-exponent (`-1 VND`), 3-decimal (`-9.223.372.036.854.775,808 KWD`), 4-decimal CLF, unknown currency defaults to 2, int64 max/min exact. Decimal `scaleb` is exact within 28 digits, ample for bigint minor units; worker-thread Decimal context is the default.
- `language_for`: `vi`, `VI`, `vi-VN`, `vi_VN.UTF-8` -> vi; `None`, `""`, `en-US` -> en. Profile locale is validated (`LocaleTag`) so odd forms are not reachable.
- Language is resolved after `_paid_plan` authz; extra `session.get(User)` reads only the caller's row inside the request transaction; no external I/O added; rendering still happens off-transaction in a thread.
- Kitty name: `names[None]` override keeps English "Kitty" identical to old `FUND`.

## Unresolved questions

1. Is there an app-side Vietnamese glossary (no vi strings found in `~/beluno/beluno_app`)? Terms here (`Quỹ chung`, `Phần chịu`, `Hạng mục`, `Hoá/Hóa`, `Tỉ/Tỷ`) will become the de facto glossary; worth fixing now.
2. Should the English date change (ISO -> `12 Mar 2027`) in the expense table/footer be called out in the PR?
3. M1 fix choice: literal-name mapping vs. a deletion marker on the participant projection?
