"""Financial ledger: currencies, ledger heads, accounts, expenses, settlements, budgets, fund.

Revision ID: 000005_financial_ledger
Revises: 000004_sync_kernel
Create Date: 2026-10-06

Forward action: creates the ``finance`` tables, seeds ``finance.currencies`` from
ISO 4217 (minor-unit exponents pinned here), and adds:

* composite plan-scoped keys so every expense, posting, settlement, budget, and
  fund row stays inside one plan and one currency;
* BEFORE UPDATE/DELETE triggers that make canonical history append-only
  (accounts, FX snapshots, revisions, payers, splits, refunds, transactions,
  postings, fund movements);
* ``DEFERRABLE INITIALLY DEFERRED`` constraint triggers that verify at commit
  that payers and splits add up to the revision amount, that every transaction
  sums to zero per currency, that expense, refund, settlement, conversion,
  waiver, and fund postings have exactly the shape their source row implies,
  and that reversals mirror their source (moved to surviving participants when
  a placeholder was merged);
* RLS limiting every finance row to active participants of its plan, and
  SECURITY DEFINER maintenance gates for the worker (reconciliation, balance
  rebuild).

Runtime roles get SELECT/INSERT on canonical tables and SELECT/INSERT/UPDATE only
on the mutable heads, balances, expenses, settlements, budgets, commitments,
and fund settings; nobody gets DELETE.

Lock/scan risk: none on existing data; every statement creates a new object.

Validation:
    SELECT count(*) FROM finance.currencies;                                  -- 157
    SELECT relname FROM pg_class WHERE relnamespace = 'finance'::regnamespace
      AND relkind = 'r' AND NOT relrowsecurity;                               -- no rows
    SELECT has_table_privilege('api_runtime', 'finance.ledger_postings', 'UPDATE'); -- f

Compatibility: additive; the previous build never reads these tables.

Rollback: forward-only. Disable finance writes with ``BELUNO_FINANCE_WRITES_ENABLED``
and keep reads available; never delete postings or reverse history as a
deployment rollback. Corrections are new entries or forward migrations.
"""

from alembic import op

revision = "000005_financial_ledger"
down_revision = "000004_sync_kernel"
branch_labels = None
depends_on = None

MAX_MINOR = 1_000_000_000_000
CATEGORIES = (
    "('food', 'lodging', 'transport', 'activities', 'shopping', 'groceries', 'fees', 'other')"
)

# ISO 4217 active currencies. Everything not listed with another exponent uses 2.
ZERO_DECIMAL = ("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF").split()  # noqa: SIM905
THREE_DECIMAL = "BHD IQD JOD KWD LYD OMR TND".split()  # noqa: SIM905
TWO_DECIMAL = (
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BRL BSD BTN "
    "BWP BYN BZD CAD CDF CHF CNY COP CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD "
    "FKP GBP GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR "
    "KPW KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN "
    "MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD "
    "SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD "
    "TZS UAH USD UYU UZS VED VES WST XCD XCG YER ZAR ZMW ZWG"
).split()  # noqa: SIM905


def _currency_rows() -> str:
    exponents = {
        **dict.fromkeys(TWO_DECIMAL, 2),
        **dict.fromkeys(ZERO_DECIMAL, 0),
        **dict.fromkeys(THREE_DECIMAL, 3),
    }
    return ",\n".join(
        f"    ('{code}', {exponent}, 'supported', 1, '2026-10-06T00:00:00Z')"
        for code, exponent in sorted(exponents.items())
    )


TABLES_SQL = f"""
CREATE TABLE finance.currencies (
    code char(3) PRIMARY KEY CHECK (code ~ '^[A-Z]{{3}}$'),
    exponent smallint NOT NULL CHECK (exponent BETWEEN 0 AND 4),
    state text NOT NULL CHECK (state IN ('supported', 'retired')),
    metadata_version integer NOT NULL CHECK (metadata_version > 0),
    updated_at timestamptz NOT NULL
);
INSERT INTO finance.currencies (code, exponent, state, metadata_version, updated_at) VALUES
{_currency_rows()};

CREATE TABLE finance.plan_ledger_heads (
    plan_id uuid PRIMARY KEY REFERENCES plans.plans (id),
    ledger_seq bigint NOT NULL CHECK (ledger_seq >= 0),
    status text NOT NULL CHECK (status IN ('open', 'settled', 'reopened')),
    disputed_settlements integer NOT NULL CHECK (disputed_settlements >= 0),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE finance.ledger_accounts (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    kind text NOT NULL CHECK (kind IN ('participant', 'fund')),
    participant_id uuid,
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    created_at timestamptz NOT NULL,
    UNIQUE (plan_id, id, currency),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id),
    CHECK ((kind = 'participant') = (participant_id IS NOT NULL))
);
CREATE UNIQUE INDEX ledger_accounts_participant_key
    ON finance.ledger_accounts (plan_id, participant_id, currency) WHERE kind = 'participant';
CREATE UNIQUE INDEX ledger_accounts_fund_key
    ON finance.ledger_accounts (plan_id, currency) WHERE kind = 'fund';

CREATE TABLE finance.account_balances (
    account_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL,
    currency char(3) NOT NULL,
    balance_minor bigint NOT NULL,
    last_ledger_seq bigint NOT NULL CHECK (last_ledger_seq >= 0),
    updated_at timestamptz NOT NULL,
    FOREIGN KEY (plan_id, account_id, currency)
        REFERENCES finance.ledger_accounts (plan_id, id, currency)
);
CREATE INDEX account_balances_plan_idx ON finance.account_balances (plan_id);

CREATE TABLE finance.fx_snapshots (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    base_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    quote_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    rate numeric(28, 12) NOT NULL CHECK (rate > 0 AND rate <= 1000000000),
    source text NOT NULL CHECK (source IN ('manual', 'estimated', 'agreed')),
    rounding_mode text NOT NULL CHECK (rounding_mode = 'half_even'),
    as_of timestamptz NOT NULL,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    CHECK (base_currency <> quote_currency)
);

CREATE TABLE finance.cost_commitments (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    source_type text NOT NULL CHECK (
        source_type IN ('manual', 'booking', 'itinerary_item', 'place', 'responsibility')
    ),
    source_id uuid NOT NULL,
    commitment_kind text NOT NULL CHECK (commitment_kind ~ '^[a-z][a-z0-9_]{{0,39}}$'),
    state text NOT NULL CHECK (
        state IN ('estimated', 'committed', 'converted_to_expense', 'cancelled', 'refunded')
    ),
    converted_from_state text CHECK (converted_from_state IN ('estimated', 'committed')),
    category text NOT NULL CHECK (category IN {CATEGORIES}),
    description text NOT NULL CHECK (char_length(btrim(description)) BETWEEN 1 AND 200),
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND {MAX_MINOR}),
    base_amount_minor bigint CHECK (base_amount_minor BETWEEN 0 AND {MAX_MINOR}),
    base_fx_snapshot_id uuid,
    expense_id uuid,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    UNIQUE (plan_id, source_type, source_id, commitment_kind),
    FOREIGN KEY (plan_id, base_fx_snapshot_id) REFERENCES finance.fx_snapshots (plan_id, id),
    CHECK ((state IN ('converted_to_expense', 'refunded')) = (expense_id IS NOT NULL)),
    CHECK ((state IN ('converted_to_expense', 'refunded')) = (converted_from_state IS NOT NULL))
);
CREATE UNIQUE INDEX cost_commitments_expense_key
    ON finance.cost_commitments (expense_id) WHERE expense_id IS NOT NULL;

CREATE TABLE finance.expenses (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    state text NOT NULL CHECK (state IN ('active', 'voided')),
    current_revision_id uuid NOT NULL,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    voided_at timestamptz,
    UNIQUE (plan_id, id),
    CHECK ((state = 'voided') = (voided_at IS NOT NULL))
);

CREATE TABLE finance.expense_revisions (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL,
    expense_id uuid NOT NULL,
    revision_number integer NOT NULL CHECK (revision_number > 0),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND {MAX_MINOR}),
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    description text NOT NULL CHECK (char_length(btrim(description)) BETWEEN 1 AND 200),
    category text NOT NULL CHECK (category IN {CATEGORIES}),
    occurred_on date NOT NULL,
    notes text CHECK (char_length(notes) BETWEEN 1 AND 2000),
    split_method text NOT NULL CHECK (
        split_method IN ('equal', 'exact', 'percentage', 'shares', 'itemized')
    ),
    split_algorithm text NOT NULL CHECK (split_algorithm = 'lr-v1'),
    split_input jsonb NOT NULL,
    base_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    base_amount_minor bigint CHECK (base_amount_minor BETWEEN 0 AND {MAX_MINOR}),
    base_fx_snapshot_id uuid,
    commitment_id uuid,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    UNIQUE (expense_id, id),
    UNIQUE (expense_id, revision_number),
    FOREIGN KEY (plan_id, expense_id) REFERENCES finance.expenses (plan_id, id),
    FOREIGN KEY (plan_id, base_fx_snapshot_id) REFERENCES finance.fx_snapshots (plan_id, id),
    FOREIGN KEY (plan_id, commitment_id) REFERENCES finance.cost_commitments (plan_id, id),
    CHECK (currency <> base_currency OR (base_amount_minor = amount_minor
                                         AND base_fx_snapshot_id IS NULL)),
    CHECK (base_fx_snapshot_id IS NULL OR base_amount_minor IS NOT NULL)
);

ALTER TABLE finance.expenses
    ADD CONSTRAINT expenses_current_revision_fkey
    FOREIGN KEY (id, current_revision_id) REFERENCES finance.expense_revisions (expense_id, id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE finance.cost_commitments
    ADD CONSTRAINT cost_commitments_expense_fkey
    FOREIGN KEY (plan_id, expense_id) REFERENCES finance.expenses (plan_id, id);

CREATE TABLE finance.expense_payers (
    revision_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    position smallint NOT NULL CHECK (position BETWEEN 0 AND 99),
    participant_id uuid,
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND {MAX_MINOR}),
    PRIMARY KEY (revision_id, position),
    UNIQUE NULLS NOT DISTINCT (revision_id, participant_id),
    FOREIGN KEY (plan_id, revision_id) REFERENCES finance.expense_revisions (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);

CREATE TABLE finance.expense_splits (
    revision_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    position smallint NOT NULL CHECK (position BETWEEN 0 AND 99),
    participant_id uuid NOT NULL,
    owed_minor bigint NOT NULL CHECK (owed_minor BETWEEN 0 AND {MAX_MINOR}),
    PRIMARY KEY (revision_id, position),
    UNIQUE (revision_id, participant_id),
    FOREIGN KEY (plan_id, revision_id) REFERENCES finance.expense_revisions (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);

CREATE TABLE finance.expense_refunds (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL,
    expense_id uuid NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND {MAX_MINOR}),
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    recipient_participant_id uuid,
    note text CHECK (char_length(note) BETWEEN 1 AND 500),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, expense_id) REFERENCES finance.expenses (plan_id, id),
    FOREIGN KEY (plan_id, recipient_participant_id)
        REFERENCES plans.plan_participants (plan_id, id)
);
CREATE INDEX expense_refunds_expense_idx ON finance.expense_refunds (expense_id);

CREATE TABLE finance.refund_shares (
    refund_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    participant_id uuid NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND {MAX_MINOR}),
    PRIMARY KEY (refund_id, participant_id),
    FOREIGN KEY (plan_id, refund_id) REFERENCES finance.expense_refunds (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);

CREATE TABLE finance.settlements (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    kind text NOT NULL CHECK (kind IN ('payment', 'waiver')),
    from_participant_id uuid NOT NULL,
    to_participant_id uuid NOT NULL,
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND {MAX_MINOR}),
    paid_currency char(3) REFERENCES finance.currencies (code),
    paid_amount_minor bigint CHECK (paid_amount_minor BETWEEN 1 AND {MAX_MINOR}),
    fx_snapshot_id uuid,
    method text CHECK (method IN ('cash', 'bank_transfer', 'card', 'mobile_payment', 'other')),
    fee_minor bigint CHECK (fee_minor BETWEEN 1 AND {MAX_MINOR}),
    note text CHECK (char_length(note) BETWEEN 1 AND 500),
    occurred_on date NOT NULL,
    status text NOT NULL CHECK (status IN ('recorded', 'confirmed', 'disputed', 'reversed')),
    overpaid boolean NOT NULL,
    recorded_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    confirmed_by_user_id uuid REFERENCES iam.users (id),
    confirmed_at timestamptz,
    disputed_by_user_id uuid REFERENCES iam.users (id),
    disputed_at timestamptz,
    reversed_by_user_id uuid REFERENCES iam.users (id),
    reversed_at timestamptz,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, from_participant_id) REFERENCES plans.plan_participants (plan_id, id),
    FOREIGN KEY (plan_id, to_participant_id) REFERENCES plans.plan_participants (plan_id, id),
    FOREIGN KEY (plan_id, fx_snapshot_id) REFERENCES finance.fx_snapshots (plan_id, id),
    CHECK (from_participant_id <> to_participant_id),
    CHECK ((paid_currency IS NULL) = (paid_amount_minor IS NULL)
           AND (paid_currency IS NULL) = (fx_snapshot_id IS NULL)),
    CHECK (paid_currency IS NULL OR paid_currency <> currency),
    CHECK (kind = 'payment' OR (paid_currency IS NULL AND method IS NULL AND fee_minor IS NULL)),
    CHECK ((status = 'reversed') = (reversed_at IS NOT NULL)),
    CHECK (status <> 'confirmed' OR confirmed_at IS NOT NULL),
    CHECK (status <> 'disputed' OR disputed_at IS NOT NULL)
);

CREATE TABLE finance.fund_settings (
    plan_id uuid PRIMARY KEY REFERENCES finance.plan_ledger_heads (plan_id),
    custodian_participant_id uuid,
    note text CHECK (char_length(note) BETWEEN 1 AND 500),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    FOREIGN KEY (plan_id, custodian_participant_id)
        REFERENCES plans.plan_participants (plan_id, id)
);

CREATE TABLE finance.fund_movements (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    kind text NOT NULL CHECK (kind IN ('contribution', 'withdrawal')),
    participant_id uuid NOT NULL,
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND {MAX_MINOR}),
    note text CHECK (char_length(note) BETWEEN 1 AND 500),
    occurred_on date NOT NULL,
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);

CREATE TABLE finance.ledger_transactions (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    ledger_seq bigint NOT NULL CHECK (ledger_seq > 0),
    kind text NOT NULL CHECK (kind IN (
        'expense', 'expense_reversal', 'refund', 'settlement', 'settlement_reversal',
        'fund_contribution', 'fund_withdrawal', 'adjustment', 'conversion'
    )),
    subtype text CHECK (subtype IN ('waiver', 'merge_transfer', 'fund_adjustment', 'correction')),
    expense_id uuid,
    revision_id uuid,
    refund_id uuid,
    settlement_id uuid,
    fund_movement_id uuid,
    reverses_transaction_id uuid,
    memo text CHECK (char_length(memo) BETWEEN 1 AND 500),
    created_by_user_id uuid REFERENCES iam.users (id),
    operation_id uuid,
    created_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    UNIQUE (plan_id, ledger_seq),
    UNIQUE (reverses_transaction_id),
    FOREIGN KEY (plan_id, expense_id) REFERENCES finance.expenses (plan_id, id),
    FOREIGN KEY (plan_id, revision_id) REFERENCES finance.expense_revisions (plan_id, id),
    FOREIGN KEY (plan_id, refund_id) REFERENCES finance.expense_refunds (plan_id, id),
    FOREIGN KEY (plan_id, settlement_id) REFERENCES finance.settlements (plan_id, id),
    FOREIGN KEY (plan_id, fund_movement_id) REFERENCES finance.fund_movements (plan_id, id),
    FOREIGN KEY (plan_id, reverses_transaction_id)
        REFERENCES finance.ledger_transactions (plan_id, id),
    CHECK ((kind = 'adjustment') = (subtype IS NOT NULL)),
    CHECK ((kind IN ('expense_reversal', 'settlement_reversal'))
           = (reverses_transaction_id IS NOT NULL)),
    CHECK (kind <> 'expense' OR (revision_id IS NOT NULL AND expense_id IS NOT NULL)),
    CHECK (kind <> 'refund' OR (refund_id IS NOT NULL AND expense_id IS NOT NULL)),
    CHECK (kind <> 'expense_reversal' OR expense_id IS NOT NULL),
    CHECK (kind NOT IN ('settlement', 'settlement_reversal', 'conversion')
           OR settlement_id IS NOT NULL),
    CHECK (subtype IS DISTINCT FROM 'waiver' OR settlement_id IS NOT NULL),
    CHECK (kind NOT IN ('fund_contribution', 'fund_withdrawal') OR fund_movement_id IS NOT NULL)
);
CREATE UNIQUE INDEX ledger_transactions_revision_key
    ON finance.ledger_transactions (revision_id) WHERE kind = 'expense';
CREATE UNIQUE INDEX ledger_transactions_refund_key
    ON finance.ledger_transactions (refund_id) WHERE kind = 'refund';
CREATE UNIQUE INDEX ledger_transactions_fund_movement_key
    ON finance.ledger_transactions (fund_movement_id) WHERE fund_movement_id IS NOT NULL;
CREATE INDEX ledger_transactions_expense_idx
    ON finance.ledger_transactions (expense_id) WHERE expense_id IS NOT NULL;
CREATE INDEX ledger_transactions_settlement_idx
    ON finance.ledger_transactions (settlement_id) WHERE settlement_id IS NOT NULL;

CREATE TABLE finance.ledger_postings (
    transaction_id uuid NOT NULL,
    account_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    currency char(3) NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor <> 0),
    PRIMARY KEY (transaction_id, account_id),
    FOREIGN KEY (plan_id, transaction_id) REFERENCES finance.ledger_transactions (plan_id, id),
    FOREIGN KEY (plan_id, account_id, currency)
        REFERENCES finance.ledger_accounts (plan_id, id, currency)
);
CREATE INDEX ledger_postings_account_idx ON finance.ledger_postings (account_id, transaction_id);
CREATE INDEX ledger_postings_plan_idx ON finance.ledger_postings (plan_id);

CREATE TABLE finance.budgets (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    scope text NOT NULL CHECK (scope IN ('total', 'category', 'participant', 'daily')),
    category text CHECK (category IN {CATEGORIES}),
    participant_id uuid,
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    limit_minor bigint NOT NULL CHECK (limit_minor BETWEEN 1 AND {MAX_MINOR}),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    UNIQUE (plan_id, id),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id),
    CHECK ((scope = 'category') = (category IS NOT NULL)),
    CHECK ((scope = 'participant') = (participant_id IS NOT NULL))
);
CREATE UNIQUE INDEX budgets_scope_key
    ON finance.budgets (plan_id, scope, category, participant_id) NULLS NOT DISTINCT
    WHERE deleted_at IS NULL;
"""

INVARIANTS_SQL = """
CREATE FUNCTION finance.invariant_violation(p_reason text) RETURNS void
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'ledger invariant violated: %', p_reason USING ERRCODE = 'check_violation';
END;
$$;

CREATE FUNCTION finance.reject_history_change() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'finance.% rows are append-only', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

-- The participant that finally holds a merged participant's money (itself when live).
CREATE FUNCTION finance.resolve_participant(p_plan_id uuid, p_participant_id uuid)
    RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH RECURSIVE chain (id, merged_into, depth) AS (
        SELECT id, merged_into_participant_id, 0 FROM plans.plan_participants
        WHERE plan_id = p_plan_id AND id = p_participant_id
        UNION ALL
        SELECT p.id, p.merged_into_participant_id, c.depth + 1
        FROM chain AS c
        JOIN plans.plan_participants AS p ON p.plan_id = p_plan_id AND p.id = c.merged_into
        WHERE c.depth < 16
    )
    SELECT id FROM chain WHERE merged_into IS NULL ORDER BY depth DESC LIMIT 1
$$;

CREATE FUNCTION finance.actual_postings(p_transaction_id uuid)
    RETURNS TABLE (participant_id uuid, currency char(3), amount_minor bigint)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT a.participant_id, p.currency, p.amount_minor
    FROM finance.ledger_postings AS p
    JOIN finance.ledger_accounts AS a ON a.id = p.account_id
    WHERE p.transaction_id = p_transaction_id
$$;

-- The postings a transaction must carry, derived only from its source rows.
-- Adjustments (corrections, fund adjustments, merge transfers) have no source
-- shape beyond summing to zero, which every transaction is checked for.
CREATE FUNCTION finance.expected_postings(p_transaction_id uuid)
    RETURNS TABLE (participant_id uuid, currency char(3), amount_minor bigint)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    tx finance.ledger_transactions%ROWTYPE;
    s finance.settlements%ROWTYPE;
    m finance.fund_movements%ROWTYPE;
BEGIN
    SELECT * INTO tx FROM finance.ledger_transactions WHERE id = p_transaction_id;
    IF tx.kind = 'expense' THEN
        RETURN QUERY
            SELECT x.participant_id, r.currency, sum(x.amount)::bigint
            FROM finance.expense_revisions AS r
            CROSS JOIN LATERAL (
                SELECT pa.participant_id, pa.amount_minor AS amount
                FROM finance.expense_payers AS pa WHERE pa.revision_id = r.id
                UNION ALL
                SELECT sp.participant_id, -sp.owed_minor
                FROM finance.expense_splits AS sp WHERE sp.revision_id = r.id
            ) AS x
            WHERE r.id = tx.revision_id
            GROUP BY x.participant_id, r.currency
            HAVING sum(x.amount) <> 0;
    ELSIF tx.kind = 'refund' THEN
        RETURN QUERY
            SELECT x.participant_id, f.currency, sum(x.amount)::bigint
            FROM finance.expense_refunds AS f
            CROSS JOIN LATERAL (
                SELECT f.recipient_participant_id AS participant_id, -f.amount_minor AS amount
                UNION ALL
                SELECT sh.participant_id, sh.amount_minor
                FROM finance.refund_shares AS sh WHERE sh.refund_id = f.id
            ) AS x
            WHERE f.id = tx.refund_id
            GROUP BY x.participant_id, f.currency
            HAVING sum(x.amount) <> 0;
    ELSIF tx.kind IN ('expense_reversal', 'settlement_reversal') THEN
        RETURN QUERY
            SELECT finance.resolve_participant(tx.plan_id, o.participant_id) AS resolved,
                   o.currency, (-sum(o.amount_minor))::bigint
            FROM finance.actual_postings(tx.reverses_transaction_id) AS o
            GROUP BY resolved, o.currency
            HAVING sum(o.amount_minor) <> 0;
    ELSIF tx.kind IN ('settlement', 'conversion')
          OR (tx.kind = 'adjustment' AND tx.subtype = 'waiver') THEN
        SELECT * INTO s FROM finance.settlements WHERE id = tx.settlement_id;
        IF tx.kind = 'conversion' THEN
            RETURN QUERY VALUES
                (s.from_participant_id, s.currency, s.amount_minor),
                (s.to_participant_id, s.currency, -s.amount_minor),
                (s.from_participant_id, s.paid_currency, -s.paid_amount_minor),
                (s.to_participant_id, s.paid_currency, s.paid_amount_minor);
        ELSIF tx.kind = 'settlement' AND s.paid_currency IS NOT NULL THEN
            RETURN QUERY VALUES
                (s.from_participant_id, s.paid_currency, s.paid_amount_minor),
                (s.to_participant_id, s.paid_currency, -s.paid_amount_minor);
        ELSE
            RETURN QUERY VALUES
                (s.from_participant_id, s.currency, s.amount_minor),
                (s.to_participant_id, s.currency, -s.amount_minor);
        END IF;
    ELSIF tx.kind IN ('fund_contribution', 'fund_withdrawal') THEN
        SELECT * INTO m FROM finance.fund_movements WHERE id = tx.fund_movement_id;
        IF tx.kind = 'fund_contribution' THEN
            RETURN QUERY VALUES
                (m.participant_id, m.currency, m.amount_minor),
                (NULL::uuid, m.currency, -m.amount_minor);
        ELSE
            RETURN QUERY VALUES
                (NULL::uuid, m.currency, m.amount_minor),
                (m.participant_id, m.currency, -m.amount_minor);
        END IF;
    ELSE
        RETURN QUERY SELECT * FROM finance.actual_postings(p_transaction_id);
    END IF;
END;
$$;

CREATE FUNCTION finance.verify_transaction(p_transaction_id uuid) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    tx finance.ledger_transactions%ROWTYPE;
    source_kind text;
    source_subtype text;
    head_seq bigint;
    differences integer;
BEGIN
    SELECT * INTO tx FROM finance.ledger_transactions WHERE id = p_transaction_id;
    SELECT ledger_seq INTO head_seq FROM finance.plan_ledger_heads WHERE plan_id = tx.plan_id;
    IF head_seq IS NULL OR tx.ledger_seq > head_seq THEN
        PERFORM finance.invariant_violation('transaction sequence is ahead of its ledger head');
    END IF;
    IF EXISTS (
        SELECT 1 FROM finance.ledger_postings WHERE transaction_id = p_transaction_id
        GROUP BY currency HAVING sum(amount_minor) <> 0
    ) THEN
        PERFORM finance.invariant_violation('postings do not sum to zero per currency');
    END IF;
    IF tx.reverses_transaction_id IS NOT NULL THEN
        SELECT kind, subtype INTO source_kind, source_subtype
        FROM finance.ledger_transactions WHERE id = tx.reverses_transaction_id;
        IF (tx.kind = 'expense_reversal' AND source_kind NOT IN ('expense', 'refund'))
           OR (tx.kind = 'settlement_reversal'
               AND source_kind NOT IN ('settlement', 'conversion')
               AND source_subtype IS DISTINCT FROM 'waiver') THEN
            PERFORM finance.invariant_violation('reversal does not match its source kind');
        END IF;
    END IF;
    IF tx.kind = 'settlement' AND NOT EXISTS (
        SELECT 1 FROM finance.settlements WHERE id = tx.settlement_id AND kind = 'payment'
    ) OR tx.subtype = 'waiver' AND NOT EXISTS (
        SELECT 1 FROM finance.settlements WHERE id = tx.settlement_id AND kind = 'waiver'
    ) OR tx.kind = 'conversion' AND NOT EXISTS (
        SELECT 1 FROM finance.settlements
        WHERE id = tx.settlement_id AND paid_currency IS NOT NULL
    ) THEN
        PERFORM finance.invariant_violation('transaction does not match its settlement');
    END IF;
    SELECT count(*) INTO differences FROM (
        (SELECT * FROM finance.expected_postings(p_transaction_id)
         EXCEPT ALL SELECT * FROM finance.actual_postings(p_transaction_id))
        UNION ALL
        (SELECT * FROM finance.actual_postings(p_transaction_id)
         EXCEPT ALL SELECT * FROM finance.expected_postings(p_transaction_id))
    ) AS diff;
    IF differences > 0 THEN
        PERFORM finance.invariant_violation('postings do not match their source entry');
    END IF;
END;
$$;

CREATE FUNCTION finance.verify_revision(p_revision_id uuid) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    revision finance.expense_revisions%ROWTYPE;
BEGIN
    SELECT * INTO revision FROM finance.expense_revisions WHERE id = p_revision_id;
    IF (SELECT coalesce(sum(amount_minor), 0) FROM finance.expense_payers
        WHERE revision_id = p_revision_id) <> revision.amount_minor THEN
        PERFORM finance.invariant_violation('payers do not add up to the expense amount');
    END IF;
    IF (SELECT coalesce(sum(owed_minor), 0) FROM finance.expense_splits
        WHERE revision_id = p_revision_id) <> revision.amount_minor THEN
        PERFORM finance.invariant_violation('splits do not add up to the expense amount');
    END IF;
    IF (SELECT count(*) FROM finance.ledger_transactions
        WHERE revision_id = p_revision_id AND kind = 'expense'
          AND expense_id = revision.expense_id) <> 1 THEN
        PERFORM finance.invariant_violation('revision has no expense transaction');
    END IF;
END;
$$;

CREATE FUNCTION finance.verify_refund(p_refund_id uuid) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    refund finance.expense_refunds%ROWTYPE;
BEGIN
    SELECT * INTO refund FROM finance.expense_refunds WHERE id = p_refund_id;
    IF (SELECT coalesce(sum(amount_minor), 0) FROM finance.refund_shares
        WHERE refund_id = p_refund_id) <> refund.amount_minor THEN
        PERFORM finance.invariant_violation('refund shares do not add up to the refund');
    END IF;
    IF (SELECT count(*) FROM finance.ledger_transactions
        WHERE refund_id = p_refund_id AND kind = 'refund'
          AND expense_id = refund.expense_id) <> 1 THEN
        PERFORM finance.invariant_violation('refund has no refund transaction');
    END IF;
END;
$$;

CREATE FUNCTION finance.verify_settlement(p_settlement_id uuid) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    s finance.settlements%ROWTYPE;
BEGIN
    SELECT * INTO s FROM finance.settlements WHERE id = p_settlement_id;
    IF (SELECT count(*) FROM finance.ledger_transactions
        WHERE settlement_id = p_settlement_id
          AND (kind = 'settlement' OR subtype = 'waiver')) <> 1
       OR (SELECT count(*) FROM finance.ledger_transactions
           WHERE settlement_id = p_settlement_id AND kind = 'conversion')
          <> (CASE WHEN s.paid_currency IS NULL THEN 0 ELSE 1 END) THEN
        PERFORM finance.invariant_violation('settlement transactions are incomplete');
    END IF;
END;
$$;

CREATE FUNCTION finance.verify_fund_movement(p_movement_id uuid) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (SELECT count(*) FROM finance.ledger_transactions
        WHERE fund_movement_id = p_movement_id) <> 1 THEN
        PERFORM finance.invariant_violation('fund movement has no transaction');
    END IF;
END;
$$;

CREATE FUNCTION finance.check_transaction() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_transaction(NEW.id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION finance.check_posting() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_transaction(NEW.transaction_id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION finance.check_revision() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_revision(NEW.id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION finance.check_revision_line() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_revision(NEW.revision_id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION finance.check_refund() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_refund(NEW.id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION finance.check_refund_share() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_refund(NEW.refund_id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION finance.check_settlement() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_settlement(NEW.id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION finance.check_fund_movement() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_fund_movement(NEW.id);
    RETURN NULL;
END;
$$;
"""

APPEND_ONLY_TABLES = (
    "ledger_accounts",
    "fx_snapshots",
    "expense_revisions",
    "expense_payers",
    "expense_splits",
    "expense_refunds",
    "refund_shares",
    "fund_movements",
    "ledger_transactions",
    "ledger_postings",
)

TRIGGERS_SQL = (
    "\n".join(
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON finance.{table}\n"
        f"    FOR EACH ROW EXECUTE FUNCTION finance.reject_history_change();"
        for table in APPEND_ONLY_TABLES
    )
    + """
CREATE CONSTRAINT TRIGGER ledger_transactions_balanced
    AFTER INSERT ON finance.ledger_transactions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_transaction();
CREATE CONSTRAINT TRIGGER ledger_postings_balanced
    AFTER INSERT ON finance.ledger_postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_posting();
CREATE CONSTRAINT TRIGGER expense_revisions_complete
    AFTER INSERT ON finance.expense_revisions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_revision();
CREATE CONSTRAINT TRIGGER expense_payers_complete
    AFTER INSERT ON finance.expense_payers DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_revision_line();
CREATE CONSTRAINT TRIGGER expense_splits_complete
    AFTER INSERT ON finance.expense_splits DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_revision_line();
CREATE CONSTRAINT TRIGGER expense_refunds_complete
    AFTER INSERT ON finance.expense_refunds DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_refund();
CREATE CONSTRAINT TRIGGER refund_shares_complete
    AFTER INSERT ON finance.refund_shares DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_refund_share();
CREATE CONSTRAINT TRIGGER settlements_complete
    AFTER INSERT ON finance.settlements DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_settlement();
CREATE CONSTRAINT TRIGGER fund_movements_complete
    AFTER INSERT ON finance.fund_movements DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_fund_movement();
"""
)

MAINTENANCE_SQL = """
CREATE FUNCTION finance.ledger_plan_ids(p_after uuid, p_limit integer) RETURNS SETOF uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT plan_id FROM finance.plan_ledger_heads
    WHERE p_after IS NULL OR plan_id > p_after
    ORDER BY plan_id
    LIMIT least(greatest(p_limit, 1), 1000)
$$;

-- Recomputes one plan from canonical rows and reports every disagreement:
-- unbalanced transactions, projection drift, and ledger sequence gaps.
CREATE FUNCTION finance.reconcile_plan(p_plan_id uuid)
    RETURNS TABLE (problem text, account_id uuid, expected_minor bigint, actual_minor bigint)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT 'unbalanced_transaction', NULL::uuid, 0::bigint, sum(p.amount_minor)::bigint
    FROM finance.ledger_postings AS p
    WHERE p.plan_id = p_plan_id
    GROUP BY p.transaction_id, p.currency
    HAVING sum(p.amount_minor) <> 0
    UNION ALL
    SELECT 'balance_drift', a.id, coalesce(t.total, 0)::bigint, b.balance_minor
    FROM finance.ledger_accounts AS a
    LEFT JOIN (
        SELECT p.account_id, sum(p.amount_minor) AS total
        FROM finance.ledger_postings AS p WHERE p.plan_id = p_plan_id
        GROUP BY p.account_id
    ) AS t ON t.account_id = a.id
    LEFT JOIN finance.account_balances AS b ON b.account_id = a.id
    WHERE a.plan_id = p_plan_id
      AND (b.account_id IS NULL OR b.balance_minor <> coalesce(t.total, 0))
    UNION ALL
    SELECT 'sequence_gap', NULL::uuid, h.ledger_seq,
           (SELECT count(*) FROM finance.ledger_transactions AS t WHERE t.plan_id = p_plan_id)
    FROM finance.plan_ledger_heads AS h
    WHERE h.plan_id = p_plan_id
      AND (h.ledger_seq <> (SELECT count(*) FROM finance.ledger_transactions AS t
                            WHERE t.plan_id = p_plan_id)
           OR h.ledger_seq <> (SELECT coalesce(max(t.ledger_seq), 0)
                               FROM finance.ledger_transactions AS t
                               WHERE t.plan_id = p_plan_id))
$$;

-- Rebuilds one plan's balance projection from postings (the shadow), returns the
-- accounts that differ, and applies the rebuilt values only when asked. The
-- ledger head is locked so no writer can interleave.
CREATE FUNCTION finance.rebuild_balances(p_plan_id uuid, p_apply boolean)
    RETURNS TABLE (account_id uuid, recorded_minor bigint, rebuilt_minor bigint)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    PERFORM 1 FROM finance.plan_ledger_heads WHERE plan_id = p_plan_id FOR UPDATE;
    RETURN QUERY
        SELECT a.id, b.balance_minor, coalesce(t.total, 0)::bigint
        FROM finance.ledger_accounts AS a
        LEFT JOIN (
            SELECT p.account_id, sum(p.amount_minor) AS total
            FROM finance.ledger_postings AS p WHERE p.plan_id = p_plan_id
            GROUP BY p.account_id
        ) AS t ON t.account_id = a.id
        LEFT JOIN finance.account_balances AS b ON b.account_id = a.id
        WHERE a.plan_id = p_plan_id
          AND (b.account_id IS NULL OR b.balance_minor <> coalesce(t.total, 0))
        ORDER BY a.id;
    IF p_apply THEN
        INSERT INTO finance.account_balances AS b (
            account_id, plan_id, currency, balance_minor, last_ledger_seq, updated_at
        )
        SELECT a.id, a.plan_id, a.currency, coalesce(t.total, 0), coalesce(t.last_seq, 0), now()
        FROM finance.ledger_accounts AS a
        LEFT JOIN (
            SELECT p.account_id, sum(p.amount_minor) AS total, max(x.ledger_seq) AS last_seq
            FROM finance.ledger_postings AS p
            JOIN finance.ledger_transactions AS x ON x.id = p.transaction_id
            WHERE p.plan_id = p_plan_id
            GROUP BY p.account_id
        ) AS t ON t.account_id = a.id
        WHERE a.plan_id = p_plan_id
        ON CONFLICT ON CONSTRAINT account_balances_pkey DO UPDATE
            SET balance_minor = EXCLUDED.balance_minor,
                last_ledger_seq = EXCLUDED.last_ledger_seq,
                updated_at = EXCLUDED.updated_at
            WHERE b.balance_minor <> EXCLUDED.balance_minor;
    END IF;
END;
$$;
"""

FINANCE_TABLES = (
    "plan_ledger_heads",
    "ledger_accounts",
    "account_balances",
    "fx_snapshots",
    "cost_commitments",
    "expenses",
    "expense_revisions",
    "expense_payers",
    "expense_splits",
    "expense_refunds",
    "refund_shares",
    "settlements",
    "fund_settings",
    "fund_movements",
    "ledger_transactions",
    "ledger_postings",
    "budgets",
)
MUTABLE_TABLES = (
    "plan_ledger_heads",
    "account_balances",
    "cost_commitments",
    "expenses",
    "settlements",
    "fund_settings",
    "budgets",
)

RLS_SQL = (
    "ALTER TABLE finance.currencies ENABLE ROW LEVEL SECURITY;\n"
    "CREATE POLICY currencies_select ON finance.currencies FOR SELECT\n"
    "    TO api_runtime, worker_runtime USING (true);\n"
    + "\n".join(
        f"ALTER TABLE finance.{table} ENABLE ROW LEVEL SECURITY;\n"
        f"CREATE POLICY {table}_select ON finance.{table} FOR SELECT TO api_runtime\n"
        f"    USING (plans.actor_is_active_participant(plan_id));\n"
        f"CREATE POLICY {table}_insert ON finance.{table} FOR INSERT TO api_runtime\n"
        f"    WITH CHECK (plans.actor_is_active_participant(plan_id));"
        for table in FINANCE_TABLES
    )
    + "\n"
    + "\n".join(
        f"CREATE POLICY {table}_update ON finance.{table} FOR UPDATE TO api_runtime\n"
        f"    USING (plans.actor_is_active_participant(plan_id))\n"
        f"    WITH CHECK (plans.actor_is_active_participant(plan_id));"
        for table in MUTABLE_TABLES
    )
)

GRANTS_SQL = f"""
GRANT SELECT ON finance.currencies TO api_runtime, worker_runtime;
GRANT SELECT, INSERT ON {", ".join(f"finance.{t}" for t in FINANCE_TABLES)} TO api_runtime;
GRANT UPDATE ON {", ".join(f"finance.{t}" for t in MUTABLE_TABLES)} TO api_runtime;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA finance FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    finance.ledger_plan_ids(uuid, integer),
    finance.reconcile_plan(uuid),
    finance.rebuild_balances(uuid, boolean)
    TO worker_runtime;
"""


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(INVARIANTS_SQL)
    op.execute(TRIGGERS_SQL)
    op.execute(MAINTENANCE_SQL)
    op.execute(RLS_SQL)
    op.execute(GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("The financial ledger is forward-only")
