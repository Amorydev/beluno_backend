"""Money alignment: expense time, the adjustment split, and readable revision history.

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

Lock/scan risk: ``ALTER TABLE`` takes ACCESS EXCLUSIVE on
``finance.expense_revisions``, ``finance.plan_ledger_heads``, and
``finance.fund_settings`` briefly. New columns are nullable or carry a constant
default (catalog-only, no rewrite, no trigger runs); the default is dropped
right after. Replacing the split-method check scans the table once.

Validation:
    SELECT count(*) FROM finance.expense_revisions WHERE source IS NULL;          -- 0
    SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'expense_revisions_split_method_check';                     -- has 'adjustment'
    SELECT count(*) FROM finance.plan_ledger_heads
    WHERE count_personal_spend IS NULL OR settle_tolerance_minor <> 0;          -- 0
    SELECT relrowsecurity FROM pg_class
    WHERE oid = 'finance.ledger_confirmations'::regclass;                       -- t
    SELECT relrowsecurity FROM pg_class WHERE oid = 'finance.fund_counts'::regclass; -- t

Compatibility: additive for stored data. The API adds request and response
fields; old clients that send none of them keep working.

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


def upgrade() -> None:
    op.execute(REVISIONS_SQL)
    op.execute(SETTINGS_SQL)
    op.execute(MERGE_STATUS_SQL)
    op.execute(KITTY_SQL)


def downgrade() -> None:
    raise RuntimeError("Money alignment is forward-only")
