"""Sign-in, token refresh, logout, and the public signing keys."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Request, Response, status

from beluno.api.dependencies import (
    ActorDep,
    OptionalActorDep,
    RuntimeDep,
    client_subject,
)
from beluno.api.problems import problem_responses
from beluno.contracts.errors import authentication_failed
from beluno.contracts.iam import (
    DeviceRequest,
    EmailChallengeRequest,
    EmailChallengeResponse,
    EmailVerifyRequest,
    ExternalSignInRequest,
    JwksResponse,
    RefreshRequest,
    TokenResponse,
    UserProfileResponse,
)
from beluno.db.models.iam import User
from beluno.modules.context import open_context
from beluno.modules.iam import email_challenges, rate_limits
from beluno.modules.iam.external_identity import IdentityProvider, VerifiedIdentity
from beluno.modules.iam.sessions import DeviceInfo, IssuedTokens, find_session, refresh_session
from beluno.modules.iam.sessions import revoke_session as revoke_auth_session
from beluno.modules.iam.sign_in import AuthenticationRequest, authenticate
from beluno.modules.plans.guest_claims import transfer_guest_participations
from beluno.token_hashing import normalize_email

router = APIRouter(tags=["auth"])

SIGN_IN_ERRORS = problem_responses(401, 403, 409, 422, 429, 503)


def profile_response(user: User) -> UserProfileResponse:
    return UserProfileResponse.model_validate(
        {
            "id": user.id,
            "kind": user.kind,
            "display_name": user.display_name,
            "email": user.email,
            "locale": user.locale,
            "timezone": user.timezone,
            "version": user.version,
        }
    )


def token_response(tokens: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        expires_at=tokens.access_token_expires_at,
        refresh_token=tokens.refresh_token,
        session_id=tokens.session_id,
        user=profile_response(tokens.user),
    )


def device_info(device: DeviceRequest | None) -> DeviceInfo:
    if device is None:
        return DeviceInfo()
    return DeviceInfo(
        client_device_id=device.client_device_id,
        label=device.label,
        platform=device.platform,
        app_version=device.app_version,
    )


async def _complete(
    runtime: RuntimeDep,
    actor: OptionalActorDep,
    identity: VerifiedIdentity,
    device: DeviceRequest | None,
    merge: bool,
) -> TokenResponse:
    async with open_context(runtime, actor) as ctx:
        tokens = await authenticate(
            ctx,
            AuthenticationRequest(
                identity=identity,
                device=device_info(device),
                merge_guest_participations=merge,
            ),
            transfer_guest_participations,
        )
    return token_response(tokens)


async def _sign_in_external(
    provider: IdentityProvider,
    body: ExternalSignInRequest,
    request: Request,
    runtime: RuntimeDep,
    actor: OptionalActorDep,
) -> TokenResponse:
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.EXTERNAL_SIGN_IN_PER_CLIENT, client_subject(request)
    )
    identity = await runtime.identity_verifier.verify(
        provider, body.id_token, nonce=body.nonce, display_name=body.display_name
    )
    return await _complete(runtime, actor, identity, body.device, body.merge_guest_participations)


@router.post("/v1/auth/google", response_model=TokenResponse, responses=SIGN_IN_ERRORS)
async def sign_in_with_google(
    body: ExternalSignInRequest,
    request: Request,
    runtime: RuntimeDep,
    actor: OptionalActorDep,
) -> TokenResponse:
    return await _sign_in_external(IdentityProvider.GOOGLE, body, request, runtime, actor)


@router.post("/v1/auth/apple", response_model=TokenResponse, responses=SIGN_IN_ERRORS)
async def sign_in_with_apple(
    body: ExternalSignInRequest,
    request: Request,
    runtime: RuntimeDep,
    actor: OptionalActorDep,
) -> TokenResponse:
    return await _sign_in_external(IdentityProvider.APPLE, body, request, runtime, actor)


@router.post(
    "/v1/auth/email/challenges",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EmailChallengeResponse,
    responses=problem_responses(422, 429, 503),
)
async def start_email_challenge(
    body: EmailChallengeRequest,
    request: Request,
    runtime: RuntimeDep,
) -> EmailChallengeResponse:
    """Always accepted for a well-formed address; never reveals whether an account exists."""

    email = normalize_email(body.email)
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.EMAIL_CHALLENGE_PER_CLIENT, client_subject(request)
    )
    await rate_limits.enforce_rate_limit(runtime, rate_limits.EMAIL_CHALLENGE_PER_EMAIL, email)
    async with open_context(runtime) as ctx:
        challenge = await email_challenges.start_challenge(ctx, email)
    return EmailChallengeResponse(challenge_id=challenge.id, expires_at=challenge.expires_at)


@router.post("/v1/auth/email/verify", response_model=TokenResponse, responses=SIGN_IN_ERRORS)
async def verify_email_challenge(
    body: EmailVerifyRequest,
    request: Request,
    runtime: RuntimeDep,
    actor: OptionalActorDep,
) -> TokenResponse:
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.EMAIL_VERIFY_PER_CLIENT, client_subject(request)
    )
    # One transaction: a wrong code still commits its attempt count, while a
    # rejected sign-in (e.g. merge consent needed) leaves the challenge unused.
    tokens: IssuedTokens | None = None
    async with open_context(runtime, actor) as ctx:
        if body.link_token is not None:
            identity = await email_challenges.verify_link(ctx, body.link_token)
        else:
            assert body.challenge_id is not None and body.code is not None
            identity = await email_challenges.verify_code(ctx, body.challenge_id, body.code)
        if identity is not None:
            tokens = await authenticate(
                ctx,
                AuthenticationRequest(
                    identity=replace(identity, display_name=body.display_name),
                    device=device_info(body.device),
                    merge_guest_participations=body.merge_guest_participations,
                ),
                transfer_guest_participations,
            )
    if tokens is None:
        raise authentication_failed()
    return token_response(tokens)


@router.post(
    "/v1/auth/refresh",
    response_model=TokenResponse,
    responses=problem_responses(401, 422, 429, 503),
)
async def refresh_tokens(
    body: RefreshRequest,
    request: Request,
    runtime: RuntimeDep,
) -> TokenResponse:
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.REFRESH_PER_CLIENT, client_subject(request)
    )
    async with open_context(runtime) as ctx:
        tokens = await refresh_session(ctx, body.refresh_token)
    if tokens is None:
        raise authentication_failed()
    return token_response(tokens)


@router.post(
    "/v1/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(401, 503),
)
async def logout(runtime: RuntimeDep, actor: ActorDep) -> Response:
    async with open_context(runtime, actor) as ctx:
        current = await find_session(ctx, ctx.require_actor().session_id)
        if current is not None:
            await revoke_auth_session(ctx, current, reason="logout")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/.well-known/jwks.json", response_model=JwksResponse, responses=problem_responses(503))
async def signing_keys(runtime: RuntimeDep) -> JwksResponse:
    return JwksResponse(keys=runtime.tokens.jwks()["keys"])
