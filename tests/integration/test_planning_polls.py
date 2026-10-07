"""Polls: who votes, changing votes, ties, quorum, one result however it closes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, finance_plan, join_with_invite, pull_all
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher
from beluno.worker import tasks

pytestmark = pytest.mark.integration


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))


@pytest.fixture
async def worker(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    database = Database.for_worker(live_settings)
    runtime = Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    monkeypatch.setattr(tasks, "get_worker_runtime", lambda: runtime)
    yield runtime
    await database.close()


async def open_poll(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, **body: Any
) -> dict[str, Any]:
    poll: dict[str, Any] = await ok(
        await api.post(
            trip.path("/polls"),
            json={
                "question": "Dinner on Friday?",
                "options": [{"label": "Ramen"}, {"label": "Sushi"}, {"label": "Yakitori"}],
                **body,
            },
            headers=user.headers,
        ),
        201,
    )
    return poll


async def cast(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, poll: dict[str, Any], label: str
) -> httpx.Response:
    [option] = [o for o in poll["options"] if o["label"] == label]
    return await api.put(
        trip.path(f"/polls/{poll['id']}/vote"),
        json={"option_id": option["id"]},
        headers=user.headers,
    )


async def test_the_electorate_votes_openly_and_one_result_stands(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, trip: FinancePlan
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    poll = await open_poll(api, bea, trip)
    # Ann, Bea, and Dan; placeholder Cam is a name, not a voter.
    assert (poll["eligible"], poll["status"]) == (3, "open")
    late = await sign_in(api, identity_provider, name="Late")
    await join_with_invite(api, owner, trip.plan_id, late)
    assert (await cast(api, late, trip, poll, "Ramen")).status_code == 403

    await ok(await cast(api, owner, trip, poll, "Ramen"))
    await ok(await cast(api, dan, trip, poll, "Sushi"))
    changed = await ok(await cast(api, dan, trip, poll, "Ramen"))  # votes may change
    ramen = next(o for o in changed["options"] if o["label"] == "Ramen")
    assert sorted(ramen["voter_ids"]) == sorted([trip.people["Ann"], trip.people["Dan"]])

    close = trip.path(f"/polls/{poll['id']}/close")
    assert (await api.post(close, headers=dan.headers)).status_code == 403
    closed = await ok(await api.post(close, headers=bea.headers))
    result = closed["result"]
    assert (closed["status"], result["outcome"], result["winner_option_id"]) == (
        "closed",
        "winner",
        ramen["id"],
    )
    assert (result["voted"], result["eligible"], result["closed_by_user_id"]) == (
        2,
        3,
        bea.user_id,
    )
    assert (await ok(await api.post(close, headers=owner.headers)))["result"] == result
    late_vote = await cast(api, bea, trip, poll, "Sushi")
    assert late_vote.status_code == 409 and late_vote.json()["code"] == "POLL_CLOSED"


async def test_locked_votes_stay_and_ties_are_settled_by_an_organiser(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    place = await ok(
        await api.post(trip.path("/places"), json={"name": "Ichiran"}, headers=bea.headers), 201
    )
    poll = await open_poll(
        api,
        owner,
        trip,
        allow_vote_change=False,
        options=[{"label": "Ichiran", "place_id": place["id"]}, {"label": "Sushi bar"}],
    )
    await ok(await cast(api, owner, trip, poll, "Ichiran"))
    locked = await cast(api, owner, trip, poll, "Sushi bar")
    assert locked.status_code == 409 and locked.json()["code"] == "VOTE_LOCKED"
    await ok(await cast(api, bea, trip, poll, "Sushi bar"))
    closed = await ok(
        await api.post(trip.path(f"/polls/{poll['id']}/close"), headers=owner.headers)
    )
    assert closed["result"]["outcome"] == "tie"
    assert len(closed["result"]["tied_option_ids"]) == 2

    outcome = trip.path(f"/polls/{poll['id']}/outcome")
    unsure = await api.post(outcome, json={"action": "save_place"}, headers=owner.headers)
    assert unsure.status_code == 422
    ichiran = next(o["id"] for o in poll["options"] if o["label"] == "Ichiran")
    sushi = next(o["id"] for o in poll["options"] if o["label"] == "Sushi bar")
    saved = await ok(
        await api.post(
            outcome, json={"action": "save_place", "option_id": ichiran}, headers=owner.headers
        )
    )
    again = await ok(
        await api.post(
            outcome, json={"action": "save_place", "option_id": ichiran}, headers=owner.headers
        )
    )
    assert (
        saved["outcomes"]
        == again["outcomes"]
        == [{"action": "save_place", "option_id": ichiran, "created_entity_id": place["id"]}]
    )
    other = await api.post(
        outcome, json={"action": "save_place", "option_id": sushi}, headers=owner.headers
    )
    assert other.status_code == 409 and other.json()["code"] == "OUTCOME_ALREADY_APPLIED"
    # The tie was settled on Ichiran: planning the other option is refused.
    split = await api.post(
        outcome, json={"action": "add_to_plan", "option_id": sushi}, headers=owner.headers
    )
    assert split.status_code == 409 and split.json()["code"] == "OUTCOME_ALREADY_APPLIED"
    planned = await ok(
        await api.post(
            outcome, json={"action": "add_to_plan", "day": "2027-03-22"}, headers=owner.headers
        )
    )
    item_id = next(
        o["created_entity_id"] for o in planned["outcomes"] if o["action"] == "add_to_plan"
    )
    item = await ok(await api.get(trip.path(f"/itinerary/{item_id}"), headers=owner.headers))
    assert (item["title"], item["place_id"], item["day"]) == ("Ichiran", place["id"], "2027-03-22")
    won = await ok(await api.get(trip.path(f"/places/{place['id']}"), headers=owner.headers))
    assert won["status"] == "poll_winner"
    assert admin.scalar("SELECT count(*) FROM decisions.poll_results") == 1


async def test_yes_no_needs_its_quorum_or_a_majority(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]

    async def decide(quorum: int | None, yes: list[SignedIn], no: list[SignedIn]) -> str:
        poll = await open_poll(api, owner, trip, kind="yes_no", options=[], quorum=quorum)
        assert [(o["label"], o["answer"]) for o in poll["options"]] == [
            ("Yes", "yes"),
            ("No", "no"),
        ]
        for user in yes:
            await ok(await cast(api, user, trip, poll, "Yes"))
        for user in no:
            await ok(await cast(api, user, trip, poll, "No"))
        closed = await ok(
            await api.post(trip.path(f"/polls/{poll['id']}/close"), headers=owner.headers)
        )
        outcome: str = closed["result"]["outcome"]
        return outcome

    assert await decide(2, [owner, bea], [dan]) == "passed"
    assert await decide(3, [owner, bea], [dan]) == "failed"  # quorum not reached
    assert await decide(1, [owner], [bea, dan]) == "failed"  # quorum met, but outvoted
    assert await decide(None, [owner], [bea, dan]) == "failed"
    assert await decide(None, [owner, bea], [dan]) == "passed"
    assert await decide(None, [], []) == "no_votes"
    too_many = await api.post(
        trip.path("/polls"),
        json={"question": "Go?", "kind": "yes_no", "quorum": 4},
        headers=owner.headers,
    )
    assert too_many.status_code == 422  # only three people may vote


async def test_the_deadline_closes_a_poll_once_even_racing_an_organiser(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase, worker: Runtime
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    due = await open_poll(api, owner, trip, deadline_at=soon)
    raced = await open_poll(api, owner, trip, deadline_at=soon)
    await ok(await cast(api, bea, trip, due, "Sushi"))
    _, cursor, _ = await pull_all(api, bea, f"plan:{trip.plan_id}")
    admin.execute(
        "UPDATE decisions.polls SET deadline_at = now() - interval '1 minute' WHERE id IN (%s, %s)",
        due["id"],
        raced["id"],
    )
    # Past the deadline nobody votes, even before the job has run.
    assert (await cast(api, owner, trip, due, "Ramen")).status_code == 409
    # The organiser and the job close at the same time: each poll gets one result.
    closing, ran = await asyncio.gather(
        api.post(trip.path(f"/polls/{raced['id']}/close"), headers=owner.headers),
        tasks.close_due_poll_records.func(0),
    )
    first = await ok(closing)
    assert ran in (1, 2)
    await tasks.close_due_poll_records.func(0)
    assert await tasks.close_due_poll_records.func(0) == 0
    rows = dict(
        admin.fetch("SELECT poll_id::text, closed_by_user_id::text FROM decisions.poll_results")
    )
    assert set(rows) == {due["id"], raced["id"]} and rows[due["id"]] is None
    assert rows[raced["id"]] in (None, owner.user_id)  # whoever locked it first
    assert (await ok(await api.get(trip.path(f"/polls/{raced['id']}"), headers=owner.headers)))[
        "result"
    ] == first["result"]

    items, _, _ = await pull_all(api, bea, f"plan:{trip.plan_id}", cursor)
    by_entity = {(i["entity_type"], i["entity_id"]): i for i in items}
    assert by_entity[("poll", due["id"])]["data"]["result"]["outcome"] == "winner"
    feed = [i["data"] for i in items if i["entity_type"] == "activity_event"]
    assert sorted((e["type"], e["entity_id"], e["actor_user_id"]) for e in feed) == sorted(
        [("poll.closed", raced["id"], rows[raced["id"]]), ("poll.closed", due["id"], None)]
    )


async def test_the_database_keeps_polls_honest(
    api: httpx.AsyncClient, trip: FinancePlan, live_settings: Settings
) -> None:
    poll = await open_poll(api, trip.owner, trip)
    option = poll["options"][0]["id"]
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    bea, dan = trip.members["Bea"].user_id, trip.people["Dan"]
    attempts = [
        # A vote written for someone else.
        (
            "INSERT INTO decisions.poll_votes (poll_id, plan_id, participant_id, option_id, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, now(), now())",
            (poll["id"], trip.plan_id, dan, option),
        ),
        # Rewriting an open poll (here: its deadline, to have the job close it).
        (
            "UPDATE decisions.polls SET deadline_at = now() - interval '1 day', "
            "version = version + 1 WHERE id = %s",
            (poll["id"],),
        ),
        # Withdrawing someone else's poll.
        (
            "UPDATE decisions.polls SET deleted_at = now(), version = version + 1 WHERE id = %s",
            (poll["id"],),
        ),
        # A poll that starts closed.
        (
            "INSERT INTO decisions.polls (id, plan_id, kind, question, allow_vote_change, "
            "status, created_by_user_id, closed_at, version, created_at, updated_at) VALUES "
            "(gen_random_uuid(), %s, 'single_choice', 'x', true, 'closed', %s, now(), 1, "
            "now(), now())",
            (trip.plan_id, bea),
        ),
        # Another option, or another voter, after the poll opened.
        (
            "INSERT INTO decisions.poll_options (id, poll_id, plan_id, label, position) "
            "VALUES (gen_random_uuid(), %s, %s, 'Late idea', 9)",
            (poll["id"], trip.plan_id),
        ),
        (
            "INSERT INTO decisions.poll_electorate (poll_id, plan_id, participant_id) "
            "VALUES (%s, %s, %s)",
            (poll["id"], trip.plan_id, trip.people["Cam"]),
        ),
        # An outcome before there is a result.
        (
            "INSERT INTO decisions.poll_outcomes (poll_id, action, plan_id, result_version, "
            "option_id, created_entity_id, created_by_user_id, created_at) VALUES "
            "(%s, 'save_place', %s, 1, %s, gen_random_uuid(), %s, now())",
            (poll["id"], trip.plan_id, option, bea),
        ),
    ]
    with psycopg.connect(dsn) as connection:
        for statement, params in attempts:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (bea,))
                connection.execute(statement, params)


async def test_a_free_text_winner_becomes_one_place_on_the_plan(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner = trip.owner

    async def decided() -> str:
        poll = await open_poll(api, owner, trip)
        await ok(await cast(api, owner, trip, poll, "Sushi"))
        await ok(await api.post(trip.path(f"/polls/{poll['id']}/close"), headers=owner.headers))
        return trip.path(f"/polls/{poll['id']}/outcome")

    async def act(outcome: str, action: str) -> dict[str, Any]:
        body: dict[str, Any] = await ok(
            await api.post(outcome, json={"action": action}, headers=owner.headers)
        )
        return body

    def made(body: dict[str, Any], action: str) -> str:
        found: str = next(o["created_entity_id"] for o in body["outcomes"] if o["action"] == action)
        return found

    # Saved first, then planned: the item is at the new place.
    outcome = await decided()
    await act(outcome, "save_place")
    both = await act(outcome, "add_to_plan")
    place_path = trip.path(f"/places/{made(both, 'save_place')}")
    place = await ok(await api.get(place_path, headers=owner.headers))
    item_path = trip.path(f"/itinerary/{made(both, 'add_to_plan')}")
    item = await ok(await api.get(item_path, headers=owner.headers))
    assert (place["name"], place["status"]) == ("Sushi", "poll_winner")
    assert item["place_id"] == place["id"]
    # Planned first, then saved: the item is pointed at the place.
    outcome = await decided()
    await act(outcome, "add_to_plan")
    both = await act(outcome, "save_place")
    item_path = trip.path(f"/itinerary/{made(both, 'add_to_plan')}")
    item = await ok(await api.get(item_path, headers=owner.headers))
    assert item["place_id"] == made(both, "save_place")
