"""Money alignment: expense details, money settings, the kitty, rates, consolidation, base changes.

Revision ID: 000008_money_alignment
Revises: 000007_trip_first_realignment
Create Date: 2026-10-06

Forward action:

* ``finance.expense_revisions`` gains ``occurred_at`` and ``occurred_timezone``
  (both or neither; the API checks that ``occurred_on`` is the local date of
  ``occurred_at``), the split method ``adjustment``, and where each revision
  came from: ``source`` (``http`` or ``sync``), the device's
  ``client_created_at``, and the writing session's ``device_label``. Existing
  revisions read as ``http`` with no client time or device.
* ``finance.plan_ledger_heads`` gains the money settings ``count_personal_spend``
  (default true) and ``settle_tolerance_minor`` (base currency, 0 to 10^10,
  default 0). ``finance.transfer_merged_balances`` computes the ledger status
  with that tolerance, like the API does.
* New append-only ``finance.ledger_confirmations``: a participant confirmed the
  ledger at one ``ledger_seq`` (only the current sequence counts).
* ``finance.fund_settings`` gains an optional per-member target
  (``target_currency`` and ``target_minor``, both or neither).
* New append-only ``finance.fund_counts``: the custodian or a manager counted
  the kitty (``counted_minor``) against what the ledger expected
  (``expected_minor``). Counts post nothing.
* New append-only reference table ``finance.market_rates`` (no tenant): daily
  market rates a provider published, for offline estimates only. The API reads
  them; only the worker inserts.
* Consolidation ("settle everything in the base currency"): mutable
  ``finance.consolidations`` (active, then possibly reversed once) with
  append-only ``consolidation_rates`` (one frozen FX snapshot per converted
  currency) and ``consolidation_lines`` (per participant and currency, the
  balance moved and the base amount it became). ``ledger_transactions`` gains
  ``consolidation_id`` and the kind ``conversion_reversal``; a ``conversion``
  belongs to a cross-currency settlement or to a consolidation, never both.
  ``finance.expected_postings`` and ``finance.verify_transaction`` are replaced
  so a consolidation conversion must post exactly its lines and its reversal
  must mirror it; a new deferred trigger checks each consolidation has its one
  conversion and its lines.
* A changeable base currency: append-only ``finance.base_currency_changes``
  (numbered per plan, with the frozen old-to-new rate), a
  ``base_change_count`` on the ledger head, and the ``base_change_number`` each
  expense revision and cost commitment was valued at, so base values are read
  through the change chain. Original amounts and postings never change.
  ``finance.guard_budget`` now lets a budget's currency change (limits are
  re-denominated); existing rows read as number 0.

Lock/scan risk: ``ALTER TABLE`` takes ACCESS EXCLUSIVE briefly on
``finance.expense_revisions``, ``finance.plan_ledger_heads``,
``finance.fund_settings``, ``finance.cost_commitments``, and
``finance.ledger_transactions``. New columns are nullable or carry a constant
default (catalog-only, no rewrite, no trigger runs); the default is dropped
right after. Replacing check constraints scans each table once. Replacing
``finance.expected_postings`` and ``finance.verify_transaction`` takes effect
for transactions committed after the migration; existing rows are not
re-verified.

Validation:
    SELECT count(*) FROM finance.expense_revisions WHERE source IS NULL;          -- 0
    SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'expense_revisions_split_method_check';                     -- has 'adjustment'
    SELECT count(*) FROM finance.plan_ledger_heads
    WHERE count_personal_spend IS NULL OR settle_tolerance_minor <> 0;          -- 0
    SELECT relrowsecurity FROM pg_class
    WHERE oid = 'finance.ledger_confirmations'::regclass;                       -- t
    SELECT relrowsecurity FROM pg_class WHERE oid = 'finance.fund_counts'::regclass; -- t
    SELECT has_table_privilege('api_runtime', 'finance.market_rates', 'INSERT');   -- f
    SELECT count(*) FROM finance.ledger_transactions
    WHERE kind = 'conversion' AND settlement_id IS NULL;                        -- 0
    SELECT count(*) FROM finance.cost_commitments WHERE base_change_number <> 0;  -- 0
    SELECT count(*) FROM finance.base_currency_changes;                          -- 0

Compatibility: additive for stored data (existing rows keep their meaning).
The API adds request and response fields and endpoints; ``plan.update`` no
longer accepts ``base_currency`` (use ``POST /v1/plans/{id}/base-currency``),
accepted before launch. The API and worker must run this revision together.

Rollback: forward-only. Restore the pre-migration backup if it fails in a shared
environment.
"""

from alembic import op

revision = "000008_money_alignment"
down_revision = "000007_trip_first_realignment"
branch_labels = None
depends_on = None

REVISIONS_SQL = """
ALTER TABLE finance.expense_revisions
    ADD COLUMN occurred_at timestamptz,
    ADD COLUMN occurred_timezone text CHECK (char_length(occurred_timezone) BETWEEN 1 AND 64),
    ADD COLUMN source text NOT NULL DEFAULT 'http' CHECK (source IN ('http', 'sync')),
    ADD COLUMN client_created_at timestamptz,
    ADD COLUMN device_label text CHECK (char_length(device_label) BETWEEN 1 AND 80),
    ADD CONSTRAINT expense_revisions_occurred_pair
        CHECK ((occurred_at IS NULL) = (occurred_timezone IS NULL)),
    DROP CONSTRAINT expense_revisions_split_method_check,
    ADD CONSTRAINT expense_revisions_split_method_check CHECK (
        split_method IN ('equal', 'exact', 'percentage', 'shares', 'itemized', 'adjustment')
    );
ALTER TABLE finance.expense_revisions ALTER COLUMN source DROP DEFAULT;
"""

SETTINGS_SQL = """
ALTER TABLE finance.plan_ledger_heads
    ADD COLUMN count_personal_spend boolean NOT NULL DEFAULT true,
    ADD COLUMN settle_tolerance_minor bigint NOT NULL DEFAULT 0
        CHECK (settle_tolerance_minor BETWEEN 0 AND 10000000000);
ALTER TABLE finance.plan_ledger_heads
    ALTER COLUMN count_personal_spend DROP DEFAULT,
    ALTER COLUMN settle_tolerance_minor DROP DEFAULT;

CREATE TABLE finance.ledger_confirmations (
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    participant_id uuid NOT NULL,
    ledger_seq bigint NOT NULL CHECK (ledger_seq >= 0),
    confirmed_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    confirmed_at timestamptz NOT NULL,
    PRIMARY KEY (plan_id, participant_id, ledger_seq),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);
CREATE INDEX ledger_confirmations_seq_idx ON finance.ledger_confirmations (plan_id, ledger_seq);
CREATE TRIGGER ledger_confirmations_append_only
    BEFORE UPDATE OR DELETE ON finance.ledger_confirmations
    FOR EACH ROW EXECUTE FUNCTION finance.reject_history_change();
ALTER TABLE finance.ledger_confirmations ENABLE ROW LEVEL SECURITY;
CREATE POLICY ledger_confirmations_select ON finance.ledger_confirmations FOR SELECT
    TO api_runtime USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY ledger_confirmations_insert ON finance.ledger_confirmations FOR INSERT
    TO api_runtime WITH CHECK (plans.actor_is_active_participant(plan_id));
GRANT SELECT, INSERT ON finance.ledger_confirmations TO api_runtime;
"""

# Same gate as in 000005; only the ledger status now honours the settle tolerance
# on base-currency balances, as the API does.
MERGE_STATUS_SQL = """
CREATE OR REPLACE FUNCTION finance.transfer_merged_balances(
    p_plan_id uuid,
    p_participant_id uuid,
    p_transaction_id uuid,
    p_operation_id uuid
) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    survivor uuid;
    head finance.plan_ledger_heads%ROWTYPE;
    base char(3);
    item record;
    survivor_account uuid;
    zero boolean;
    live_settlement boolean;
    now_at timestamptz := transaction_timestamp();
BEGIN
    IF NOT plans.actor_has_participant_row(p_plan_id) THEN
        RAISE EXCEPTION 'not a participant of this plan' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT finance.resolve_participant(p_plan_id, merged_into_participant_id) INTO survivor
    FROM plans.plan_participants
    WHERE plan_id = p_plan_id AND id = p_participant_id AND access_state = 'merged';
    IF survivor IS NULL THEN
        RAISE EXCEPTION 'participant is not merged' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT * INTO head FROM finance.plan_ledger_heads WHERE plan_id = p_plan_id FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM finance.ledger_accounts AS a
        JOIN finance.account_balances AS b ON b.account_id = a.id
        WHERE a.plan_id = p_plan_id AND a.participant_id = p_participant_id
          AND b.balance_minor <> 0
    ) THEN
        RETURN NULL;
    END IF;
    SELECT base_currency INTO base FROM plans.plans WHERE id = p_plan_id;
    head.ledger_seq := head.ledger_seq + 1;
    INSERT INTO finance.ledger_transactions (
        id, plan_id, ledger_seq, kind, subtype, created_by_user_id, operation_id, created_at
    ) VALUES (
        p_transaction_id, p_plan_id, head.ledger_seq, 'adjustment', 'merge_transfer',
        iam.actor_id(), p_operation_id, now_at
    );
    FOR item IN
        SELECT a.id, a.currency, b.balance_minor
        FROM finance.ledger_accounts AS a
        JOIN finance.account_balances AS b ON b.account_id = a.id
        WHERE a.plan_id = p_plan_id AND a.participant_id = p_participant_id
          AND b.balance_minor <> 0
        ORDER BY a.currency
    LOOP
        SELECT id INTO survivor_account FROM finance.ledger_accounts
        WHERE plan_id = p_plan_id AND participant_id = survivor AND currency = item.currency;
        IF survivor_account IS NULL THEN
            -- Server-made account IDs (UUIDv4); clients never generate account IDs.
            survivor_account := gen_random_uuid();
            INSERT INTO finance.ledger_accounts (id, plan_id, kind, participant_id, currency, created_at)
            VALUES (survivor_account, p_plan_id, 'participant', survivor, item.currency, now_at);
            INSERT INTO finance.account_balances (
                account_id, plan_id, currency, balance_minor, last_ledger_seq, updated_at
            ) VALUES (survivor_account, p_plan_id, item.currency, 0, 0, now_at);
        END IF;
        INSERT INTO finance.ledger_postings (transaction_id, account_id, plan_id, currency, amount_minor)
        VALUES (p_transaction_id, item.id, p_plan_id, item.currency, -item.balance_minor),
               (p_transaction_id, survivor_account, p_plan_id, item.currency, item.balance_minor);
        UPDATE finance.account_balances
        SET balance_minor = balance_minor - item.balance_minor,
            last_ledger_seq = head.ledger_seq, updated_at = now_at
        WHERE account_id = item.id;
        UPDATE finance.account_balances
        SET balance_minor = balance_minor + item.balance_minor,
            last_ledger_seq = head.ledger_seq, updated_at = now_at
        WHERE account_id = survivor_account;
        survivor_account := NULL;
    END LOOP;
    zero := NOT EXISTS (
        SELECT 1 FROM finance.ledger_accounts AS a
        JOIN finance.account_balances AS b ON b.account_id = a.id
        WHERE a.plan_id = p_plan_id AND a.kind = 'participant'
          AND abs(b.balance_minor) > CASE WHEN a.currency = base
                                          THEN head.settle_tolerance_minor ELSE 0 END
    );
    live_settlement := EXISTS (
        SELECT 1 FROM finance.settlements WHERE plan_id = p_plan_id AND status <> 'reversed'
    );
    UPDATE finance.plan_ledger_heads
    SET ledger_seq = head.ledger_seq,
        version = head.version + 1,
        status = CASE WHEN zero AND live_settlement THEN 'settled'
                      WHEN zero OR head.status = 'open' THEN 'open'
                      ELSE 'reopened' END,
        updated_at = now_at
    WHERE plan_id = p_plan_id;
    RETURN head.version + 1;
END;
$$;
"""

KITTY_SQL = """
ALTER TABLE finance.fund_settings
    ADD COLUMN target_currency char(3) REFERENCES finance.currencies (code),
    ADD COLUMN target_minor bigint CHECK (target_minor BETWEEN 1 AND 1000000000000),
    ADD CONSTRAINT fund_settings_target_pair
        CHECK ((target_currency IS NULL) = (target_minor IS NULL));

CREATE TABLE finance.fund_counts (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    counted_minor bigint NOT NULL CHECK (counted_minor BETWEEN 0 AND 1000000000000),
    expected_minor bigint NOT NULL CHECK (expected_minor BETWEEN 0 AND 1000000000000),
    note text CHECK (char_length(note) BETWEEN 1 AND 500),
    counted_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    UNIQUE (plan_id, id)
);
CREATE INDEX fund_counts_plan_idx ON finance.fund_counts (plan_id, currency, created_at);
CREATE TRIGGER fund_counts_append_only BEFORE UPDATE OR DELETE ON finance.fund_counts
    FOR EACH ROW EXECUTE FUNCTION finance.reject_history_change();
ALTER TABLE finance.fund_counts ENABLE ROW LEVEL SECURITY;
CREATE POLICY fund_counts_select ON finance.fund_counts FOR SELECT
    TO api_runtime USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY fund_counts_insert ON finance.fund_counts FOR INSERT
    TO api_runtime WITH CHECK (plans.actor_is_active_participant(plan_id));
GRANT SELECT, INSERT ON finance.fund_counts TO api_runtime;
"""

MARKET_RATES_SQL = """
CREATE TABLE finance.market_rates (
    base_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    quote_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    rate numeric(28, 12) NOT NULL CHECK (rate > 0 AND rate <= 1000000000),
    as_of timestamptz NOT NULL,
    source text NOT NULL CHECK (char_length(source) BETWEEN 1 AND 40),
    fetched_at timestamptz NOT NULL,
    PRIMARY KEY (base_currency, quote_currency, as_of),
    CHECK (base_currency <> quote_currency)
);
CREATE TRIGGER market_rates_append_only BEFORE UPDATE OR DELETE ON finance.market_rates
    FOR EACH ROW EXECUTE FUNCTION finance.reject_history_change();
ALTER TABLE finance.market_rates ENABLE ROW LEVEL SECURITY;
CREATE POLICY market_rates_select ON finance.market_rates FOR SELECT
    TO api_runtime, worker_runtime USING (true);
CREATE POLICY market_rates_insert ON finance.market_rates FOR INSERT
    TO worker_runtime WITH CHECK (true);
GRANT SELECT ON finance.market_rates TO api_runtime, worker_runtime;
GRANT INSERT ON finance.market_rates TO worker_runtime;
"""

CONSOLIDATION_TABLES_SQL = """
CREATE TABLE finance.consolidations (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    base_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    state text NOT NULL CHECK (state IN ('active', 'reversed')),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    reversed_by_user_id uuid REFERENCES iam.users (id),
    reversed_at timestamptz,
    version integer NOT NULL CHECK (version > 0),
    updated_at timestamptz NOT NULL,
    UNIQUE (plan_id, id),
    CHECK ((state = 'reversed') = (reversed_at IS NOT NULL))
);

CREATE TABLE finance.consolidation_rates (
    consolidation_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    currency char(3) NOT NULL REFERENCES finance.currencies (code),
    fx_snapshot_id uuid NOT NULL,
    PRIMARY KEY (consolidation_id, currency),
    FOREIGN KEY (plan_id, consolidation_id) REFERENCES finance.consolidations (plan_id, id),
    FOREIGN KEY (plan_id, fx_snapshot_id) REFERENCES finance.fx_snapshots (plan_id, id)
);

-- One line per participant and converted currency: the balance moved out of
-- that currency and the base-currency amount it became.
CREATE TABLE finance.consolidation_lines (
    consolidation_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    currency char(3) NOT NULL,
    participant_id uuid NOT NULL,
    amount_minor bigint NOT NULL
        CHECK (amount_minor <> 0 AND abs(amount_minor) <= 1000000000000),
    base_amount_minor bigint NOT NULL CHECK (abs(base_amount_minor) <= 1000000000000),
    PRIMARY KEY (consolidation_id, currency, participant_id),
    FOREIGN KEY (plan_id, consolidation_id) REFERENCES finance.consolidations (plan_id, id),
    FOREIGN KEY (consolidation_id, currency)
        REFERENCES finance.consolidation_rates (consolidation_id, currency),
    FOREIGN KEY (plan_id, participant_id) REFERENCES plans.plan_participants (plan_id, id)
);

ALTER TABLE finance.ledger_transactions
    ADD COLUMN consolidation_id uuid,
    ADD CONSTRAINT ledger_transactions_consolidation_fkey
        FOREIGN KEY (plan_id, consolidation_id) REFERENCES finance.consolidations (plan_id, id),
    DROP CONSTRAINT ledger_transactions_kind_check,
    ADD CONSTRAINT ledger_transactions_kind_check CHECK (kind IN (
        'expense', 'expense_reversal', 'refund', 'settlement', 'settlement_reversal',
        'fund_contribution', 'fund_withdrawal', 'adjustment', 'conversion',
        'conversion_reversal'
    )),
    DROP CONSTRAINT ledger_transactions_check1,
    ADD CONSTRAINT ledger_transactions_check1 CHECK (
        (kind IN ('expense_reversal', 'settlement_reversal', 'conversion_reversal'))
        = (reverses_transaction_id IS NOT NULL)
    ),
    DROP CONSTRAINT ledger_transactions_check5,
    ADD CONSTRAINT ledger_transactions_check5 CHECK (
        kind NOT IN ('settlement', 'settlement_reversal') OR settlement_id IS NOT NULL
    ),
    ADD CONSTRAINT ledger_transactions_conversion_source CHECK (
        kind <> 'conversion' OR ((settlement_id IS NULL) <> (consolidation_id IS NULL))
    ),
    ADD CONSTRAINT ledger_transactions_consolidation_kind CHECK (
        (kind = 'conversion_reversal' AND consolidation_id IS NOT NULL)
        OR (kind = 'conversion')
        OR consolidation_id IS NULL
    );
CREATE UNIQUE INDEX ledger_transactions_consolidation_key
    ON finance.ledger_transactions (consolidation_id) WHERE kind = 'conversion';

CREATE TRIGGER consolidation_rates_append_only
    BEFORE UPDATE OR DELETE ON finance.consolidation_rates
    FOR EACH ROW EXECUTE FUNCTION finance.reject_history_change();
CREATE TRIGGER consolidation_lines_append_only
    BEFORE UPDATE OR DELETE ON finance.consolidation_lines
    FOR EACH ROW EXECUTE FUNCTION finance.reject_history_change();

-- A consolidation changes only by being reversed, once.
CREATE FUNCTION finance.guard_consolidation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (NEW.id, NEW.plan_id, NEW.base_currency, NEW.created_by_user_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.plan_id, OLD.base_currency, OLD.created_by_user_id, OLD.created_at)
       OR OLD.state = 'reversed' OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'finance.% does not allow this change', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER consolidations_guard BEFORE UPDATE ON finance.consolidations
    FOR EACH ROW EXECUTE FUNCTION finance.guard_consolidation();

ALTER TABLE finance.consolidations ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance.consolidation_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance.consolidation_lines ENABLE ROW LEVEL SECURITY;
CREATE POLICY consolidations_select ON finance.consolidations FOR SELECT TO api_runtime
    USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY consolidations_insert ON finance.consolidations FOR INSERT TO api_runtime
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY consolidations_update ON finance.consolidations FOR UPDATE TO api_runtime
    USING (plans.actor_is_active_participant(plan_id))
    WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY consolidation_rates_select ON finance.consolidation_rates FOR SELECT
    TO api_runtime USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY consolidation_rates_insert ON finance.consolidation_rates FOR INSERT
    TO api_runtime WITH CHECK (plans.actor_is_active_participant(plan_id));
CREATE POLICY consolidation_lines_select ON finance.consolidation_lines FOR SELECT
    TO api_runtime USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY consolidation_lines_insert ON finance.consolidation_lines FOR INSERT
    TO api_runtime WITH CHECK (plans.actor_is_active_participant(plan_id));
GRANT SELECT, INSERT, UPDATE ON finance.consolidations TO api_runtime;
GRANT SELECT, INSERT ON finance.consolidation_rates, finance.consolidation_lines TO api_runtime;
"""

# The deferred invariants of 000005, extended: a consolidation conversion must
# post exactly its lines, and a conversion reversal must mirror its conversion.
CONSOLIDATION_INVARIANTS_SQL = """
CREATE OR REPLACE FUNCTION finance.expected_postings(p_transaction_id uuid)
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
    ELSIF tx.kind IN ('expense_reversal', 'settlement_reversal', 'conversion_reversal') THEN
        RETURN QUERY
            SELECT finance.resolve_participant(tx.plan_id, o.participant_id) AS resolved,
                   o.currency, (-sum(o.amount_minor))::bigint
            FROM finance.actual_postings(tx.reverses_transaction_id) AS o
            GROUP BY resolved, o.currency
            HAVING sum(o.amount_minor) <> 0;
    ELSIF tx.kind = 'conversion' AND tx.consolidation_id IS NOT NULL THEN
        RETURN QUERY
            SELECT x.participant_id, x.currency, sum(x.amount)::bigint
            FROM (
                SELECT l.participant_id, l.currency, -l.amount_minor AS amount
                FROM finance.consolidation_lines AS l
                WHERE l.consolidation_id = tx.consolidation_id
                UNION ALL
                SELECT l.participant_id, c.base_currency, l.base_amount_minor
                FROM finance.consolidation_lines AS l
                JOIN finance.consolidations AS c ON c.id = l.consolidation_id
                WHERE l.consolidation_id = tx.consolidation_id
            ) AS x
            GROUP BY x.participant_id, x.currency
            HAVING sum(x.amount) <> 0;
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

CREATE OR REPLACE FUNCTION finance.verify_transaction(p_transaction_id uuid) RETURNS void
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
        IF EXISTS (
            SELECT 1 FROM finance.ledger_transactions AS source
            WHERE source.id = tx.reverses_transaction_id
              AND (source.expense_id IS DISTINCT FROM tx.expense_id
                   OR source.settlement_id IS DISTINCT FROM tx.settlement_id
                   OR source.consolidation_id IS DISTINCT FROM tx.consolidation_id)
        ) THEN
            PERFORM finance.invariant_violation('reversal is filed under another entry');
        END IF;
        IF (tx.kind = 'expense_reversal' AND source_kind NOT IN ('expense', 'refund'))
           OR (tx.kind = 'settlement_reversal'
               AND source_kind NOT IN ('settlement', 'conversion')
               AND source_subtype IS DISTINCT FROM 'waiver')
           OR (tx.kind = 'conversion_reversal' AND source_kind <> 'conversion') THEN
            PERFORM finance.invariant_violation('reversal does not match its source kind');
        END IF;
    END IF;
    IF tx.kind = 'settlement' AND NOT EXISTS (
        SELECT 1 FROM finance.settlements WHERE id = tx.settlement_id AND kind = 'payment'
    ) OR tx.subtype = 'waiver' AND NOT EXISTS (
        SELECT 1 FROM finance.settlements WHERE id = tx.settlement_id AND kind = 'waiver'
    ) OR tx.kind = 'conversion' AND tx.settlement_id IS NOT NULL AND NOT EXISTS (
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

CREATE FUNCTION finance.verify_consolidation(p_consolidation_id uuid) RETURNS void
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (SELECT count(*) FROM finance.ledger_transactions
        WHERE consolidation_id = p_consolidation_id AND kind = 'conversion') <> 1
       OR NOT EXISTS (
           SELECT 1 FROM finance.consolidation_lines WHERE consolidation_id = p_consolidation_id
       ) THEN
        PERFORM finance.invariant_violation('consolidation is incomplete');
    END IF;
END;
$$;

CREATE FUNCTION finance.check_consolidation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM finance.verify_consolidation(NEW.id);
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER consolidations_complete
    AFTER INSERT ON finance.consolidations DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION finance.check_consolidation();
REVOKE EXECUTE ON FUNCTION finance.guard_consolidation(), finance.verify_consolidation(uuid),
    finance.check_consolidation() FROM PUBLIC;
"""

BASE_CHANGE_SQL = """
ALTER TABLE finance.plan_ledger_heads
    ADD COLUMN base_change_count integer NOT NULL DEFAULT 0 CHECK (base_change_count >= 0);
ALTER TABLE finance.plan_ledger_heads ALTER COLUMN base_change_count DROP DEFAULT;
ALTER TABLE finance.expense_revisions
    ADD COLUMN base_change_number integer NOT NULL DEFAULT 0 CHECK (base_change_number >= 0);
ALTER TABLE finance.expense_revisions ALTER COLUMN base_change_number DROP DEFAULT;
ALTER TABLE finance.cost_commitments
    ADD COLUMN base_change_number integer NOT NULL DEFAULT 0 CHECK (base_change_number >= 0);
ALTER TABLE finance.cost_commitments ALTER COLUMN base_change_number DROP DEFAULT;

CREATE TABLE finance.base_currency_changes (
    plan_id uuid NOT NULL REFERENCES finance.plan_ledger_heads (plan_id),
    change_number integer NOT NULL CHECK (change_number > 0),
    from_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    to_currency char(3) NOT NULL REFERENCES finance.currencies (code),
    fx_snapshot_id uuid NOT NULL,
    ledger_seq bigint NOT NULL CHECK (ledger_seq >= 0),
    created_by_user_id uuid NOT NULL REFERENCES iam.users (id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (plan_id, change_number),
    FOREIGN KEY (plan_id, fx_snapshot_id) REFERENCES finance.fx_snapshots (plan_id, id),
    CHECK (from_currency <> to_currency)
);
CREATE TRIGGER base_currency_changes_append_only
    BEFORE UPDATE OR DELETE ON finance.base_currency_changes
    FOR EACH ROW EXECUTE FUNCTION finance.reject_history_change();
ALTER TABLE finance.base_currency_changes ENABLE ROW LEVEL SECURITY;
CREATE POLICY base_currency_changes_select ON finance.base_currency_changes FOR SELECT
    TO api_runtime USING (plans.actor_is_active_participant(plan_id));
CREATE POLICY base_currency_changes_insert ON finance.base_currency_changes FOR INSERT
    TO api_runtime WITH CHECK (plans.actor_is_active_participant(plan_id));
GRANT SELECT, INSERT ON finance.base_currency_changes TO api_runtime;

-- Budget limits are re-denominated when the base currency changes, so their
-- currency may now change too; scope and identity stay fixed.
CREATE OR REPLACE FUNCTION finance.guard_budget() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (NEW.id, NEW.plan_id, NEW.scope, NEW.category, NEW.participant_id,
        NEW.created_by_user_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.plan_id, OLD.scope, OLD.category, OLD.participant_id,
        OLD.created_by_user_id, OLD.created_at)
       OR OLD.deleted_at IS NOT NULL OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'finance.% does not allow this change', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.execute(REVISIONS_SQL)
    op.execute(SETTINGS_SQL)
    op.execute(MERGE_STATUS_SQL)
    op.execute(KITTY_SQL)
    op.execute(MARKET_RATES_SQL)
    op.execute(CONSOLIDATION_TABLES_SQL)
    op.execute(CONSOLIDATION_INVARIANTS_SQL)
    op.execute(BASE_CHANGE_SQL)


def downgrade() -> None:
    raise RuntimeError("Money alignment is forward-only")
