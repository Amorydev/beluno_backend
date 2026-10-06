"""Model-based ledger check: random finance histories against a reference model.

Hypothesis drives sequences of expenses, revisions, voids, refunds, settlements,
reversals, fund flows, fund-paid expenses, a placeholder merge, and settling
everything in the base currency (and undoing it) through the public API. A
small reference model applies the same accepted commands to the pure money
kernel. After every history the server's balances must equal the
model's, every currency must sum to zero, merged participants must hold nothing,
the fund must never be overdrawn, and the reconciler must find no drift.
Examples are derandomized, so a failure reproduces from the printed example.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from beluno.api.main import create_app
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.finance.fx import convert
from beluno.modules.finance.postings import (
    FUND,
    Party,
    consolidation_amounts,
    expense_postings,
    refund_allocation,
    refund_postings,
    transfer_postings,
)
from beluno.modules.finance.splits import Payer, Share, largest_remainder
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, finance_plan, if_match, ledger_balances
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

PEOPLE = ("Ann", "Bea", "Cam", "Dee")
CURRENCIES = ("USD", "JPY")
# USD cents for one yen, frozen when everything is settled in USD.
JPY_RATE = "0.0067"
EXPECTED_REFUSALS = {
    "CONSOLIDATION_SETTLED",
    "FUND_INSUFFICIENT",
    "REFUND_EXCEEDS_AMOUNT",
    "INVALID_STATE_TRANSITION",
    "PARTICIPANT_NOT_ELIGIBLE",
    "VALIDATION_FAILED",
}

Step = tuple[str, int, int, int, int]
steps = st.lists(
    st.tuples(
        st.sampled_from(
            (
                "expense",
                "fund_expense",
                "revise",
                "void",
                "refund",
                "settle",
                "reverse",
                "contribute",
                "withdraw",
                "merge",
                "consolidate",
                "unconsolidate",
            )
        ),
        st.integers(0, 3),
        st.integers(1, 15),
        st.integers(1, 5_000),
        st.integers(0, 1),
    ),
    min_size=3,
    max_size=14,
)


@dataclass
class ModelExpense:
    id: str
    version: int
    payer: str | None
    consumers: list[str]
    amount: int
    currency: str
    live: bool = True
    refunds: list[tuple[str | None, list[Share]]] = field(default_factory=list)

    def shares(self) -> list[Share]:
        owed = largest_remainder(self.amount, [1] * len(self.consumers))
        return [Share(UUID(pid), value) for pid, value in zip(self.consumers, owed, strict=True)]


@dataclass
class ModelSettlement:
    id: str
    version: int
    debtor: str
    creditor: str
    amount: int
    currency: str
    live: bool = True


@dataclass
class ModelConversion:
    id: str
    version: int
    postings: dict[str, dict[Party, int]]
    live: bool = True


class Model:
    def __init__(self, trip: FinancePlan) -> None:
        self.trip = trip
        self.expenses: list[ModelExpense] = []
        self.settlements: list[ModelSettlement] = []
        self.flows: list[tuple[str, str, int, str]] = []
        self.conversions: list[ModelConversion] = []
        self.merged: dict[str, str] = {}

    def person(self, index: int) -> str:
        return self.trip.people[PEOPLE[index % len(PEOPLE)]]

    def resolve(self, party: Party) -> Party:
        if party.participant_id is None:
            return party
        current = str(party.participant_id)
        while current in self.merged:
            current = self.merged[current]
        return Party(UUID(current))

    def balances(self) -> dict[tuple[str | None, str], int]:
        totals: dict[tuple[str | None, str], int] = defaultdict(int)

        def add(currency: str, postings: dict[Party, int]) -> None:
            for party, amount in postings.items():
                holder = self.resolve(party)
                key = (str(holder.participant_id) if holder.participant_id else None, currency)
                totals[key] += amount

        for expense in self.expenses:
            if not expense.live:
                continue
            payer = Payer(UUID(expense.payer) if expense.payer else None, expense.amount)
            add(expense.currency, expense_postings([payer], expense.shares()))
            for recipient, shares in expense.refunds:
                party = Party(UUID(recipient)) if recipient else FUND
                add(expense.currency, refund_postings(party, shares))
        for settlement in self.settlements:
            if settlement.live:
                add(
                    settlement.currency,
                    transfer_postings(
                        Party(UUID(settlement.debtor)),
                        Party(UUID(settlement.creditor)),
                        settlement.amount,
                    ),
                )
        for kind, person, amount, currency in self.flows:
            party = Party(UUID(person))
            add(
                currency,
                transfer_postings(party, FUND, amount)
                if kind == "contribution"
                else transfer_postings(FUND, party, amount),
            )
        for conversion in self.conversions:
            if conversion.live:
                for currency, postings in conversion.postings.items():
                    add(currency, postings)
        return {key: value for key, value in totals.items() if value != 0}

    def consolidation(self) -> dict[str, dict[Party, int]]:
        """The conversion settling every yen balance in USD, as the server must post it."""

        yen = {
            UUID(pid): value
            for (pid, currency), value in self.balances().items()
            if currency == "JPY" and pid is not None
        }
        total = convert(
            sum(value for value in yen.values() if value > 0),
            from_exponent=0,
            to_exponent=2,
            rate=Decimal(JPY_RATE),
        )
        dollars = consolidation_amounts(yen, total)
        return {
            "JPY": {Party(pid): -value for pid, value in yen.items()},
            "USD": {Party(pid): value for pid, value in dollars.items()},
        }


class Driver:
    def __init__(self, api: httpx.AsyncClient, trip: FinancePlan, model: Model) -> None:
        self.api = api
        self.trip = trip
        self.model = model
        self.owner = trip.owner

    def accepted(self, response: httpx.Response) -> bool:
        if response.status_code in (200, 201):
            return True
        assert response.status_code in (409, 412, 422), response.text
        assert response.json()["code"] in EXPECTED_REFUSALS, response.text
        return False

    async def run(self, step: Step) -> None:
        action, who, count, amount, currency_index = step
        model = self.model
        currency = CURRENCIES[currency_index]
        if action in ("expense", "fund_expense"):
            consumers = [model.person(who + offset) for offset in range(1 + count % 4)]
            consumers = list(dict.fromkeys(consumers))
            payer = None if action == "fund_expense" else model.person(who + count)
            body = {
                "description": "Spend",
                "occurred_on": "2026-10-06",
                "amount_minor": amount,
                "currency": currency,
                "payers": [
                    {"fund": True, "amount_minor": amount}
                    if payer is None
                    else {"participant_id": payer, "amount_minor": amount}
                ],
                "split": {"method": "equal", "participant_ids": consumers},
            }
            response = await self.api.post(
                self.trip.path("/expenses"), json=body, headers=self.owner.headers
            )
            if self.accepted(response):
                data = response.json()
                model.expenses.append(
                    ModelExpense(data["id"], data["version"], payer, consumers, amount, currency)
                )
            return
        if action in ("revise", "void", "refund"):
            live = [expense for expense in model.expenses if expense.live]
            if not live:
                return
            expense = live[count % len(live)]
            path = self.trip.path(f"/expenses/{expense.id}")
            headers = if_match(expense.version, self.owner)
            if action == "void":
                response = await self.api.post(path + "/void", headers=headers)
                if self.accepted(response):
                    expense.live = False
                    expense.version = response.json()["version"]
                return
            if action == "refund":
                recipient = expense.payer
                response = await self.api.post(
                    path + "/refunds",
                    json={
                        "amount_minor": 1 + amount % expense.amount,
                        "recipient": {"fund": True}
                        if recipient is None
                        else {"participant_id": recipient},
                    },
                    headers=headers,
                )
                if self.accepted(response):
                    refund = 1 + amount % expense.amount
                    expense.refunds.append((recipient, refund_allocation(refund, expense.shares())))
                    expense.version = response.json()["version"]
                return
            body = {
                "description": "Revised",
                "occurred_on": "2026-10-06",
                "amount_minor": amount,
                "currency": expense.currency,
                "payers": [
                    {"fund": True, "amount_minor": amount}
                    if expense.payer is None
                    else {"participant_id": expense.payer, "amount_minor": amount}
                ],
                "split": {"method": "equal", "participant_ids": expense.consumers},
            }
            response = await self.api.put(path, json=body, headers=headers)
            if self.accepted(response):
                expense.amount = amount
                expense.version = response.json()["version"]
            return
        if action == "settle":
            debtor, creditor = model.person(who), model.person(who + count)
            response = await self.api.post(
                self.trip.path("/settlements"),
                json={
                    "from_participant_id": debtor,
                    "to_participant_id": creditor,
                    "currency": currency,
                    "amount_minor": amount,
                    "occurred_on": "2026-10-06",
                },
                headers=self.owner.headers,
            )
            if self.accepted(response):
                data = response.json()
                model.settlements.append(
                    ModelSettlement(data["id"], data["version"], debtor, creditor, amount, currency)
                )
            return
        if action == "reverse":
            live_settlements = [s for s in model.settlements if s.live]
            if not live_settlements:
                return
            settlement = live_settlements[count % len(live_settlements)]
            response = await self.api.post(
                self.trip.path(f"/settlements/{settlement.id}/reverse"),
                headers=if_match(settlement.version, self.owner),
            )
            if self.accepted(response):
                settlement.live = False
            return
        if action in ("contribute", "withdraw"):
            person = model.person(who)
            path = "/fund/contributions" if action == "contribute" else "/fund/withdrawals"
            response = await self.api.post(
                self.trip.path(path),
                json={
                    "participant_id": person,
                    "currency": currency,
                    "amount_minor": amount,
                    "occurred_on": "2026-10-06",
                },
                headers=self.owner.headers,
            )
            if self.accepted(response):
                kind = "contribution" if action == "contribute" else "withdrawal"
                model.flows.append((kind, person, amount, currency))
            return
        if action == "consolidate":
            # The kitty pays out its yen first, then everything is settled in USD.
            held = -model.balances().get((None, "JPY"), 0)
            if held > 0:
                await self.run(("withdraw", who, count, held, CURRENCIES.index("JPY")))
            expected = model.consolidation()
            response = await self.api.post(
                self.trip.path("/ledger/consolidations"),
                json={"rates": [{"currency": "JPY", "rate": JPY_RATE}]},
                headers=self.owner.headers,
            )
            if self.accepted(response):
                data = response.json()
                model.conversions.append(ModelConversion(data["id"], data["version"], expected))
            return
        if action == "unconsolidate":
            live_conversions = [c for c in model.conversions if c.live]
            if not live_conversions:
                return
            conversion = live_conversions[-1]
            response = await self.api.post(
                self.trip.path(f"/ledger/consolidations/{conversion.id}/reverse"),
                headers=if_match(conversion.version, self.owner),
            )
            if self.accepted(response):
                conversion.live = False
            return
        # merge: Bea claims the "Dee" placeholder once, merging it into her row.
        dee, bea = self.trip.people["Dee"], self.trip.people["Bea"]
        if dee in model.merged:
            return
        token = await self.api.post(
            self.trip.path(f"/participants/{dee}/claim-invites"),
            json={},
            headers=self.owner.headers,
        )
        merged = await self.api.post(
            "/v1/invites/redeem",
            json={"token": token.json()["token"], "merge_existing": True},
            headers=self.trip.members["Bea"].headers,
        )
        assert merged.status_code == 200, merged.text
        model.merged[dee] = bea


async def run_example(
    live_settings: Settings,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    script: list[Step],
) -> Model:
    admin.truncate_all()
    database = Database(live_settings)
    app = create_app(
        settings=live_settings,
        database=database,
        identity_verifier=identity_provider.verifier(live_settings),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as api:
            trip = await finance_plan(
                api, identity_provider, admin, members=("Bea",), placeholders=("Cam", "Dee")
            )
            model = Model(trip)
            driver = Driver(api, trip, model)
            # Seed the fund so fund-paid expenses and withdrawals usually succeed.
            for currency_index in range(len(CURRENCIES)):
                await driver.run(("contribute", 0, 1, 5_000, currency_index))
            for step in script:
                await driver.run(step)
            assert await ledger_balances(api, trip.owner, trip) == model.balances()
    finally:
        await database.close()

    plan_id = trip.plan_id
    assert admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", plan_id) == []
    unbalanced = admin.fetch(
        "SELECT currency, sum(balance_minor) FROM finance.account_balances WHERE plan_id = %s "
        "GROUP BY currency HAVING sum(balance_minor) <> 0",
        plan_id,
    )
    assert unbalanced == []
    overdrawn = admin.scalar(
        "SELECT count(*) FROM finance.ledger_accounts a JOIN finance.account_balances b "
        "ON b.account_id = a.id WHERE a.plan_id = %s AND a.kind = 'fund' AND b.balance_minor > 0",
        plan_id,
    )
    assert overdrawn == 0
    holding_merged = admin.scalar(
        "SELECT count(*) FROM finance.ledger_accounts a "
        "JOIN finance.account_balances b ON b.account_id = a.id "
        "JOIN plans.plan_participants p ON p.id = a.participant_id "
        "WHERE a.plan_id = %s AND p.access_state = 'merged' AND b.balance_minor <> 0",
        plan_id,
    )
    assert holding_merged == 0
    return model


@given(script=steps)
@settings(
    max_examples=20,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_random_finance_histories_match_the_reference_model(
    live_settings: Settings,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    script: list[Step],
) -> None:
    asyncio.run(run_example(live_settings, identity_provider, admin, script))


def test_handwritten_history_with_merge_refunds_and_fund(
    live_settings: Settings, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    script: list[Step] = [
        ("contribute", 0, 1, 4000, 0),
        ("expense", 1, 3, 1000, 0),
        ("fund_expense", 3, 3, 2999, 0),
        ("refund", 0, 1, 700, 0),
        ("expense", 3, 2, 777, 1),
        ("settle", 3, 1, 250, 0),
        ("merge", 0, 0, 1, 0),
        ("revise", 0, 0, 1500, 0),
        ("void", 0, 1, 1, 0),
        ("reverse", 0, 0, 1, 0),
        ("withdraw", 0, 0, 500, 0),
        ("refund", 0, 0, 100, 1),
        ("consolidate", 1, 0, 1, 0),
        ("expense", 2, 1, 333, 1),
        ("unconsolidate", 0, 0, 1, 0),
        ("consolidate", 2, 0, 1, 0),
        ("settle", 1, 1, 90, 0),
        ("unconsolidate", 0, 0, 1, 0),
    ]
    model = asyncio.run(run_example(live_settings, identity_provider, admin, script))
    # The first consolidation was undone; the second stays because of the later payment.
    assert [conversion.live for conversion in model.conversions] == [False, True]
