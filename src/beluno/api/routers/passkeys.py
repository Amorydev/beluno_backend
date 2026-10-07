"""Passkeys: add one (Me), sign in or step up with one (Welcome), list, rename, remove."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response, status

from beluno.api.dependencies import ActorDep, OptionalActorDep, RuntimeDep, client_subject
from beluno.api.presenters import device_info, token_response
from beluno.api.problems import problem_responses
from beluno.contracts.errors import BelunoError, authentication_failed, validation_error
from beluno.contracts.iam import TokenResponse
from beluno.contracts.passkeys import (
    PasskeyOptionsResponse,
    PasskeyRegistrationRequest,
    PasskeyRenameRequest,
    PasskeyResponse,
    PasskeySignInRequest,
)
from beluno.db.models.iam import Passkey
from beluno.modules.context import open_context
from beluno.modules.iam import passkeys, rate_limits
from beluno.modules.iam.sessions import IssuedTokens
from beluno.modules.iam.sign_in import AuthenticationRequest, authenticate
from beluno.modules.plans.guest_claims import transfer_guest_participations

router = APIRouter(tags=["passkeys"])

SIGN_IN_ERRORS = problem_responses(401, 403, 409, 422, 429, 503)
MANAGE_ERRORS = problem_responses(401, 403, 404, 422, 429, 503)


@router.post(
    "/v1/me/passkeys/registration-options",
    response_model=PasskeyOptionsResponse,
    responses=MANAGE_ERRORS,
)
async def passkey_registration_options(
    runtime: RuntimeDep, actor: ActorDep
) -> PasskeyOptionsResponse:
    """Start adding a passkey (registered accounts, recent sign-in)."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.PASSKEY_REGISTRATION_PER_USER, str(actor.user_id)
    )
    async with open_context(runtime, actor) as ctx:
        options = await passkeys.registration_options(ctx)
    return PasskeyOptionsResponse(challenge_id=options.challenge_id, public_key=options.public_key)


@router.post(
    "/v1/me/passkeys",
    status_code=status.HTTP_201_CREATED,
    response_model=PasskeyResponse,
    responses=MANAGE_ERRORS,
)
async def add_passkey(
    body: PasskeyRegistrationRequest, runtime: RuntimeDep, actor: ActorDep
) -> PasskeyResponse:
    """Finish adding a passkey with the device's response."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.PASSKEY_REGISTRATION_PER_USER, str(actor.user_id)
    )
    async with open_context(runtime, actor) as ctx:
        added = await passkeys.register(ctx, body.challenge_id, body.credential, body.label)
    # A response that does not verify still used up its challenge (committed above).
    if added is None:
        raise validation_error("the passkey could not be verified; start again")
    return passkey_response(added)


@router.get("/v1/me/passkeys", response_model=list[PasskeyResponse], responses=MANAGE_ERRORS)
async def list_passkeys(runtime: RuntimeDep, actor: ActorDep) -> list[PasskeyResponse]:
    async with open_context(runtime, actor) as ctx:
        rows = await passkeys.list_passkeys(ctx)
    return [passkey_response(row) for row in rows]


@router.patch(
    "/v1/me/passkeys/{passkey_id}", response_model=PasskeyResponse, responses=MANAGE_ERRORS
)
async def rename_passkey(
    passkey_id: UUID, body: PasskeyRenameRequest, runtime: RuntimeDep, actor: ActorDep
) -> PasskeyResponse:
    async with open_context(runtime, actor) as ctx:
        renamed = await passkeys.rename(ctx, passkey_id, body.label)
    return passkey_response(renamed)


@router.delete(
    "/v1/me/passkeys/{passkey_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=MANAGE_ERRORS,
)
async def remove_passkey(passkey_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> Response:
    """Remove a passkey (recent sign-in required)."""

    async with open_context(runtime, actor) as ctx:
        await passkeys.remove(ctx, passkey_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/v1/auth/passkey/options", response_model=PasskeyOptionsResponse, responses=SIGN_IN_ERRORS
)
async def passkey_sign_in_options(
    request: Request, runtime: RuntimeDep, actor: OptionalActorDep
) -> PasskeyOptionsResponse:
    """A challenge to sign in (any passkey of this app) or step up (one of yours)."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.PASSKEY_OPTIONS_PER_CLIENT, client_subject(request)
    )
    async with open_context(runtime, actor) as ctx:
        options = await passkeys.authentication_options(ctx)
    return PasskeyOptionsResponse(challenge_id=options.challenge_id, public_key=options.public_key)


@router.post("/v1/auth/passkey", response_model=TokenResponse, responses=SIGN_IN_ERRORS)
async def sign_in_with_passkey(
    body: PasskeySignInRequest, request: Request, runtime: RuntimeDep, actor: OptionalActorDep
) -> TokenResponse:
    """Sign in, step up, or (as a guest) claim the passkey's account."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.PASSKEY_SIGN_IN_PER_CLIENT, client_subject(request)
    )
    # The used-up challenge and the new counter always commit: a response that fails
    # to verify, or a sign-in refused afterwards (merge consent needed, another
    # account), can never be sent again. Only the refused sign-in itself rolls back.
    tokens: IssuedTokens | None = None
    refused: BelunoError | None = None
    async with open_context(runtime, actor) as ctx:
        identity = await passkeys.verify(ctx, body.challenge_id, body.credential)
        if identity is not None:
            try:
                async with ctx.savepoint():
                    tokens = await authenticate(
                        ctx,
                        AuthenticationRequest(
                            identity=identity,
                            device=device_info(body.device),
                            merge_guest_participations=body.merge_guest_participations,
                        ),
                        transfer_guest_participations,
                    )
            except BelunoError as error:
                refused = error
    if refused is not None:
        raise refused
    if tokens is None:
        raise authentication_failed()
    return token_response(tokens)


def passkey_response(passkey: Passkey) -> PasskeyResponse:
    return PasskeyResponse(
        id=passkey.id,
        label=passkey.label,
        backed_up=passkey.backed_up,
        transports=passkey.transports,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
    )
