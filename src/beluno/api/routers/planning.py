"""Trip planning over REST: saved places, the itinerary, and polls (trips only).

Every write is also a sync push command; reads here mirror the ``place`` and
``itinerary_item``, and ``poll`` sync entities.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import planning as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.http import IdempotencyKey, IfMatch, command_call, finish, finish_empty, set_etag
from beluno.api.planning_projection import item_response, place_response, poll_response
from beluno.api.problems import problem_responses
from beluno.contracts.planning import (
    AddPlaceToPlanRequest,
    AttendanceRequest,
    ItineraryItemCreateRequest,
    ItineraryItemRequest,
    ItineraryItemResponse,
    PlaceCreateRequest,
    PlaceReactionRequest,
    PlaceRequest,
    PlaceResponse,
    PollCreateRequest,
    PollOutcomeRequest,
    PollResponse,
    VoteRequest,
)
from beluno.modules.context import open_context
from beluno.modules.planning import itinerary, places, polls
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/plans/{plan_id}", tags=["planning"])

READ_ERRORS = problem_responses(401, 404, 409, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 429, 503)


@router.get("/places", response_model=list[PlaceResponse], responses=READ_ERRORS)
async def list_places(plan_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> list[PlaceResponse]:
    async with open_context(runtime, actor) as ctx:
        views = await places.list_places(ctx, plan_id)
    return [place_response(view) for view in views]


@router.post(
    "/places",
    status_code=status.HTTP_201_CREATED,
    response_model=PlaceResponse,
    responses=WRITE_ERRORS,
)
async def create_place(
    plan_id: UUID,
    body: PlaceCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PlaceResponse:
    """Save a place; a Maps link is read for coordinates offline, never fetched."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLACE_CREATE, call, body))


@router.get("/places/{place_id}", response_model=PlaceResponse, responses=READ_ERRORS)
async def get_place(
    plan_id: UUID, place_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> PlaceResponse:
    async with open_context(runtime, actor) as ctx:
        view = await places.get_place(ctx, plan_id, place_id)
    set_etag(response, view.place.version)
    return place_response(view)


@router.put("/places/{place_id}", response_model=PlaceResponse, responses=WRITE_ERRORS)
async def update_place(
    plan_id: UUID,
    place_id: UUID,
    body: PlaceRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> PlaceResponse:
    """Replace the place (whoever saved it, or an organizer)."""

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id, place_id=place_id)
    return finish(response, await runner.run(actor, commands.PLACE_UPDATE, call, body))


@router.delete("/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS)
async def delete_place(
    plan_id: UUID,
    place_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    call = command_call(idempotency_key, plan_id=plan_id, place_id=place_id)
    return finish_empty(await runner.run(actor, commands.PLACE_DELETE, call, EmptyPayload()))


@router.put("/places/{place_id}/reaction", response_model=PlaceResponse, responses=WRITE_ERRORS)
async def react_to_place(
    plan_id: UUID,
    place_id: UUID,
    body: PlaceReactionRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PlaceResponse:
    """Say whether you want to go ("3 of 6 want to go")."""

    call = command_call(idempotency_key, plan_id=plan_id, place_id=place_id)
    return finish(response, await runner.run(actor, commands.PLACE_REACT, call, body))


@router.post(
    "/places/{place_id}/add-to-plan",
    status_code=status.HTTP_201_CREATED,
    response_model=ItineraryItemResponse,
    responses=WRITE_ERRORS,
)
async def add_place_to_plan(
    plan_id: UUID,
    place_id: UUID,
    body: AddPlaceToPlanRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ItineraryItemResponse:
    """Put the place on the itinerary; the place becomes ``in_plan``."""

    call = command_call(idempotency_key, plan_id=plan_id, place_id=place_id)
    return finish(response, await runner.run(actor, commands.PLACE_ADD_TO_PLAN, call, body))


@router.get("/itinerary", response_model=list[ItineraryItemResponse], responses=READ_ERRORS)
async def list_itinerary(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> list[ItineraryItemResponse]:
    """Items by day (anytime last), then by their order within the day."""

    async with open_context(runtime, actor) as ctx:
        views = await itinerary.list_items(ctx, plan_id)
    return [item_response(view) for view in views]


@router.post(
    "/itinerary",
    status_code=status.HTTP_201_CREATED,
    response_model=ItineraryItemResponse,
    responses=WRITE_ERRORS,
)
async def create_itinerary_item(
    plan_id: UUID,
    body: ItineraryItemCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ItineraryItemResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.ITEM_CREATE, call, body))


@router.get("/itinerary/{item_id}", response_model=ItineraryItemResponse, responses=READ_ERRORS)
async def get_itinerary_item(
    plan_id: UUID, item_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> ItineraryItemResponse:
    async with open_context(runtime, actor) as ctx:
        view = await itinerary.get_item(ctx, plan_id, item_id)
    set_etag(response, view.item.version)
    return item_response(view)


@router.put("/itinerary/{item_id}", response_model=ItineraryItemResponse, responses=WRITE_ERRORS)
async def update_itinerary_item(
    plan_id: UUID,
    item_id: UUID,
    body: ItineraryItemRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> ItineraryItemResponse:
    """Replace the item: move it (``day``, ``order_key``), retime it, mark it done or cancelled."""

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id, item_id=item_id)
    return finish(response, await runner.run(actor, commands.ITEM_UPDATE, call, body))


@router.delete(
    "/itinerary/{item_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS
)
async def delete_itinerary_item(
    plan_id: UUID,
    item_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    call = command_call(idempotency_key, plan_id=plan_id, item_id=item_id)
    return finish_empty(await runner.run(actor, commands.ITEM_DELETE, call, EmptyPayload()))


@router.put(
    "/itinerary/{item_id}/attendance",
    response_model=ItineraryItemResponse,
    responses=WRITE_ERRORS,
)
async def attend_itinerary_item(
    plan_id: UUID,
    item_id: UUID,
    body: AttendanceRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ItineraryItemResponse:
    """Say whether you are going."""

    call = command_call(idempotency_key, plan_id=plan_id, item_id=item_id)
    return finish(response, await runner.run(actor, commands.ITEM_ATTEND, call, body))


@router.get("/polls", response_model=list[PollResponse], responses=READ_ERRORS)
async def list_polls(plan_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> list[PollResponse]:
    async with open_context(runtime, actor) as ctx:
        views = await polls.list_polls(ctx, plan_id)
    return [poll_response(view) for view in views]


@router.post(
    "/polls",
    status_code=status.HTTP_201_CREATED,
    response_model=PollResponse,
    responses=WRITE_ERRORS,
)
async def create_poll(
    plan_id: UUID,
    body: PollCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PollResponse:
    """Open a poll; everyone active in the trip now may vote on it."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.POLL_CREATE, call, body))


@router.get("/polls/{poll_id}", response_model=PollResponse, responses=READ_ERRORS)
async def get_poll(
    plan_id: UUID, poll_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> PollResponse:
    async with open_context(runtime, actor) as ctx:
        view = await polls.get_poll(ctx, plan_id, poll_id)
    set_etag(response, view.poll.version)
    return poll_response(view)


@router.delete("/polls/{poll_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS)
async def delete_poll(
    plan_id: UUID,
    poll_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Withdraw an open poll (a closed one is kept with its result)."""

    call = command_call(idempotency_key, plan_id=plan_id, poll_id=poll_id)
    return finish_empty(await runner.run(actor, commands.POLL_DELETE, call, EmptyPayload()))


@router.put("/polls/{poll_id}/vote", response_model=PollResponse, responses=WRITE_ERRORS)
async def vote(
    plan_id: UUID,
    poll_id: UUID,
    body: VoteRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PollResponse:
    """Cast or change your vote (changes are refused when the poll locks them)."""

    call = command_call(idempotency_key, plan_id=plan_id, poll_id=poll_id)
    return finish(response, await runner.run(actor, commands.POLL_VOTE, call, body))


@router.post("/polls/{poll_id}/close", response_model=PollResponse, responses=WRITE_ERRORS)
async def close_poll(
    plan_id: UUID,
    poll_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PollResponse:
    """Close early (the creator or an organiser); the result is computed once."""

    call = command_call(idempotency_key, plan_id=plan_id, poll_id=poll_id)
    return finish(response, await runner.run(actor, commands.POLL_CLOSE, call, EmptyPayload()))


@router.post("/polls/{poll_id}/outcome", response_model=PollResponse, responses=WRITE_ERRORS)
async def apply_poll_outcome(
    plan_id: UUID,
    poll_id: UUID,
    body: PollOutcomeRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PollResponse:
    """Save the winning place or put it on the itinerary; repeating it changes nothing."""

    call = command_call(idempotency_key, plan_id=plan_id, poll_id=poll_id)
    return finish(response, await runner.run(actor, commands.POLL_APPLY_OUTCOME, call, body))
