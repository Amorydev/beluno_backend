"""The single write path of the plan ledger.

Every finance command opens the ledger the same way::

    lock the plan row (load_plan for_update) and authorize
    lock (or create) the plan's ledger head          FOR UPDATE
    validate against the participants and balances read under that lock
    append transactions: next ledger_seq, accounts, postings, balances
    finish: fund availability, ledger status, one ``ledger`` change

Postings and balances move together inside the transaction; deferred
constraint triggers re-verify every entry at commit. Lock order is always plan
row, then ledger head, then the command's own rows, so finance writers never
wait on each other in a cycle.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess, load_plan, require_plan
from beluno.authorization.policy import AccessState, PlanAction
from beluno.db.ids import new_id
from beluno.db.models.finance import (
    AccountBalance,
    Currency,
    LedgerAccount,
    LedgerHead,
    LedgerPosting,
    LedgerTransaction,
    Settlement,
)
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.finance.currencies import supported_currency
from beluno.modules.finance.errors import (
    fund_insufficient,
    not_available_for_hangout,
    participant_not_eligible,
)
from beluno.modules.finance.postings import Party, Postings, reversal_postings
from beluno.modules.finance.states import LedgerStatus, SettlementStatus, next_ledger_status
from beluno.modules.sync_audit.recorder import ChangeScope, record_change
from beluno.observability.metrics import instruments

LEDGER_ENTITY = "ledger"
TRIP = "trip"
MAX_MERGE_DEPTH = 16
# Participants who may still settle up: everyone with history except merged rows
# (their money moved to the survivor) and people never admitted.
SETTLING_STATES = frozenset({AccessState.ACTIVE, AccessState.LEFT, AccessState.REMOVED})

EntryPostings = Mapping[str, Postings]


@dataclass
class Ledger:
    ctx: CommandContext
    access: PlanAccess
    head: LedgerHead
    participants: dict[UUID, PlanParticipant]
    accounts: dict[tuple[UUID | None, str], LedgerAccount]
    balances: dict[UUID, AccountBalance]
    currencies: dict[str, Currency] = field(default_factory=dict)
    changed: bool = False

    @property
    def plan_id(self) -> UUID:
        return self.access.plan.id

    # --- references ---------------------------------------------------------------

    def require_trip(self) -> None:
        """Budgets, cost commitments, and the fund exist for trips only."""

        if self.access.plan.type != TRIP:
            raise not_available_for_hangout()

    async def currency(self, code: str) -> Currency:
        if code not in self.currencies:
            self.currencies[code] = await supported_currency(self.ctx, code)
        return self.currencies[code]

    def participant(self, participant_id: UUID, *, keep: Iterable[UUID] = ()) -> PlanParticipant:
        """A participant new entries may name: active, or already in the entry being revised."""

        participant = self.participants.get(participant_id)
        if participant is None:
            raise participant_not_eligible()
        state = AccessState(participant.access_state)
        if state is AccessState.ACTIVE:
            return participant
        if participant_id in set(keep) and state in SETTLING_STATES:
            return participant
        raise participant_not_eligible()

    def settling_party(self, participant_id: UUID) -> PlanParticipant:
        """Settlements may involve people who left or were removed, never merged rows."""

        participant = self.participants.get(participant_id)
        if participant is None or AccessState(participant.access_state) not in SETTLING_STATES:
            raise participant_not_eligible()
        return participant

    def resolve(self, participant_id: UUID) -> UUID:
        """The participant that now holds a merged participant's money (itself when live)."""

        current = participant_id
        for _ in range(MAX_MERGE_DEPTH):
            row = self.participants.get(current)
            if row is None or row.merged_into_participant_id is None:
                return current
            current = row.merged_into_participant_id
        raise RuntimeError("participant merge chain is too deep")

    def resolve_party(self, party: Party) -> Party:
        if party.participant_id is None:
            return party
        return Party(self.resolve(party.participant_id))

    # --- appending ----------------------------------------------------------------

    async def append(
        self,
        *,
        kind: str,
        postings: EntryPostings,
        subtype: str | None = None,
        memo: str | None = None,
        expense_id: UUID | None = None,
        revision_id: UUID | None = None,
        refund_id: UUID | None = None,
        settlement_id: UUID | None = None,
        fund_movement_id: UUID | None = None,
        consolidation_id: UUID | None = None,
        reverses: LedgerTransaction | None = None,
    ) -> LedgerTransaction:
        ctx = self.ctx
        if any(party.is_fund for entries in postings.values() for party in entries):
            # Fund-paid expenses, refunds to the fund, and fund adjustments included.
            self.require_trip()
        self.head.ledger_seq += 1
        transaction = LedgerTransaction(
            id=new_id(),
            plan_id=self.plan_id,
            ledger_seq=self.head.ledger_seq,
            kind=kind,
            subtype=subtype,
            expense_id=expense_id,
            revision_id=revision_id,
            refund_id=refund_id,
            settlement_id=settlement_id,
            fund_movement_id=fund_movement_id,
            consolidation_id=consolidation_id,
            reverses_transaction_id=reverses.id if reverses else None,
            memo=memo,
            created_by_user_id=ctx.actor.user_id if ctx.actor else None,
            operation_id=ctx.operation_id,
            created_at=ctx.now,
        )
        ctx.session.add(transaction)
        await ctx.session.flush()
        lines = [
            (party, currency, amount)
            for currency, entries in sorted(postings.items())
            for party, amount in entries.items()
            if amount != 0
        ]
        opened = [
            await self._open_account(party, currency)
            for party, currency, _ in lines
            if (party.participant_id, currency) not in self.accounts
        ]
        if opened:
            await ctx.session.flush()
            for account in opened:
                balance = AccountBalance(
                    account_id=account.id,
                    plan_id=self.plan_id,
                    currency=account.currency,
                    balance_minor=0,
                    last_ledger_seq=0,
                    updated_at=ctx.now,
                )
                self.balances[account.id] = balance
                ctx.session.add(balance)
        for party, currency, amount in lines:
            account = self.accounts[(party.participant_id, currency)]
            ctx.session.add(
                LedgerPosting(
                    transaction_id=transaction.id,
                    account_id=account.id,
                    plan_id=self.plan_id,
                    currency=currency,
                    amount_minor=amount,
                )
            )
            balance = self.balances[account.id]
            balance.balance_minor += amount
            balance.last_ledger_seq = transaction.ledger_seq
            balance.updated_at = ctx.now
        await ctx.session.flush()
        self.changed = True
        return transaction

    async def _open_account(self, party: Party, currency: str) -> LedgerAccount:
        await self.currency(currency)
        account = LedgerAccount(
            id=new_id(),
            plan_id=self.plan_id,
            kind="fund" if party.is_fund else "participant",
            participant_id=party.participant_id,
            currency=currency,
            created_at=self.ctx.now,
        )
        self.accounts[(party.participant_id, currency)] = account
        self.ctx.session.add(account)
        return account

    async def postings_of(self, transaction_id: UUID) -> dict[str, Postings]:
        rows = await self.ctx.session.execute(
            select(LedgerAccount.participant_id, LedgerPosting.currency, LedgerPosting.amount_minor)
            .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
            .where(LedgerPosting.transaction_id == transaction_id)
        )
        entries: dict[str, Postings] = {}
        for participant_id, currency, amount in rows:
            entries.setdefault(currency, {})[Party(participant_id)] = amount
        return entries

    async def reverse(self, original: LedgerTransaction, *, kind: str) -> LedgerTransaction:
        """Append the exact negation of ``original``, moved to surviving participants."""

        entries = await self.postings_of(original.id)
        reversed_entries = {
            currency: reversal_postings(
                postings, {party: self.resolve_party(party) for party in postings}
            )
            for currency, postings in entries.items()
        }
        return await self.append(
            kind=kind,
            postings=reversed_entries,
            expense_id=original.expense_id,
            settlement_id=original.settlement_id,
            consolidation_id=original.consolidation_id,
            reverses=original,
        )

    # --- finishing ----------------------------------------------------------------

    def balance_of(self, party: Party, currency: str) -> int:
        account = self.accounts.get((party.participant_id, currency))
        return self.balances[account.id].balance_minor if account else 0

    async def finish(self) -> None:
        """Check fund availability, move the ledger status, and record one ledger change."""

        if not self.changed:
            return
        for (participant_id, _currency), account in self.accounts.items():
            if participant_id is None and self.balances[account.id].balance_minor > 0:
                raise fund_insufficient()
        await self.update_status()
        await self.touch()

    def everyone_settled(self) -> bool:
        """Every participant is at zero; base-currency balances within the tolerance count."""

        base = self.access.plan.base_currency
        tolerance = self.head.settle_tolerance_minor
        return all(
            abs(self.balances[account.id].balance_minor) <= (tolerance if currency == base else 0)
            for (participant_id, currency), account in self.accounts.items()
            if participant_id is not None
        )

    async def update_status(self) -> None:
        live_settlements = (
            await self.ctx.session.execute(
                select(func.count())
                .select_from(Settlement)
                .where(
                    Settlement.plan_id == self.plan_id,
                    Settlement.status != SettlementStatus.REVERSED.value,
                )
            )
        ).scalar_one()
        self.head.status = next_ledger_status(
            LedgerStatus(self.head.status),
            balances_zero=self.everyone_settled(),
            has_live_settlement=live_settlements > 0,
        ).value

    async def touch(self) -> None:
        """Bump the ledger entity version (balances, status, or dispute count changed)."""

        self.head.version += 1
        self.head.updated_at = self.ctx.now
        await self.ctx.session.flush()
        await record_change(
            self.ctx,
            entity_type=LEDGER_ENTITY,
            entity_id=self.plan_id,
            entity_version=self.head.version,
            scope=ChangeScope.PLAN,
            scope_id=self.plan_id,
        )
        self.changed = False


async def open_ledger(ctx: CommandContext, plan_id: UUID, action: PlanAction) -> Ledger:
    """Authorize ``action`` and take the plan's ledger lock for one finance command."""

    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, action)
    return await ledger_for(ctx, access)


async def ledger_for(ctx: CommandContext, access: PlanAccess) -> Ledger:
    """Take the ledger lock for a plan the caller already loaded and authorized.

    Other modules reach the ledger only through finance ports, which call this
    after their own policy check (for example the planning module recording a
    booking's cost commitment). Those callers must have locked the plan row
    (``load_plan(..., for_update=True)``) so the lock order stays plan row, then
    ledger head.
    """

    plan_id = access.plan.id
    head = await lock_head(ctx, plan_id)
    participants = {
        row.id: row
        for row in (
            await ctx.session.execute(
                select(PlanParticipant)
                .where(PlanParticipant.plan_id == plan_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    accounts = {
        (row.participant_id, row.currency): row
        for row in (
            await ctx.session.execute(select(LedgerAccount).where(LedgerAccount.plan_id == plan_id))
        ).scalars()
    }
    balances = {
        row.account_id: row
        for row in (
            await ctx.session.execute(
                select(AccountBalance)
                .where(AccountBalance.plan_id == plan_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    return Ledger(
        ctx=ctx,
        access=access,
        head=head,
        participants=participants,
        accounts=accounts,
        balances=balances,
    )


async def lock_head(ctx: CommandContext, plan_id: UUID) -> LedgerHead:
    """Lock the plan's ledger head, creating it on the plan's first finance write."""

    statement = (
        select(LedgerHead)
        .where(LedgerHead.plan_id == plan_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    started = time.perf_counter()
    head = (await ctx.session.execute(statement)).scalar_one_or_none()
    instruments().ledger_lock_wait.record((time.perf_counter() - started) * 1_000)
    if head is not None:
        return head
    head = LedgerHead(
        plan_id=plan_id,
        ledger_seq=0,
        status=LedgerStatus.OPEN.value,
        disputed_settlements=0,
        count_personal_spend=True,
        settle_tolerance_minor=0,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(head)
            await ctx.session.flush()
    except IntegrityError:
        return (await ctx.session.execute(statement)).scalar_one()
    # A new ledger entity: devices that already synced the plan must learn about it
    # even when the command that created it (a budget, the fund settings) posts nothing.
    await record_change(
        ctx,
        entity_type=LEDGER_ENTITY,
        entity_id=plan_id,
        entity_version=head.version,
        scope=ChangeScope.PLAN,
        scope_id=plan_id,
    )
    return head


async def ledger_exists(ctx: CommandContext, plan_id: UUID) -> bool:
    """Whether the plan has any finance data (its base currency is then fixed)."""

    found = await ctx.session.execute(
        select(LedgerHead.plan_id).where(LedgerHead.plan_id == plan_id)
    )
    return found.scalar_one_or_none() is not None
