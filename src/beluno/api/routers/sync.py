"""Sync protocol: handshake, idempotent push batches, and cursor-based pull.

Realtime is only a wake-up hint; these endpoints are the durable protocol.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.problems import problem_responses
from beluno.api.projection import FeedProjector
from beluno.contracts.errors import BelunoError, feature_disabled, validation_error
from beluno.contracts.sync import (
    MAX_PULL_PAGE_SIZE,
    MAX_PULL_SCOPES,
    ChangeItem,
    CommandInfo,
    HandshakeRequest,
    HandshakeResponse,
    ProtocolInfo,
    PullRequest,
    PullResponse,
    PullScopeResponse,
    PushRequest,
    PushResponse,
    RetentionInfo,
    ScopeEntry,
    SyncFeatures,
    SyncLimits,
)
from beluno.modules.context import Runtime, open_context
from beluno.modules.iam import rate_limits
from beluno.sync.commands import (
    PROTOCOL_VERSION_MAX,
    PROTOCOL_VERSION_MIN,
    CommandRegistry,
    client_upgrade_required,
)
from beluno.sync.cursor import (
    CursorCodec,
    CursorError,
    decode_directory_cursor,
    encode_directory_cursor,
)
from beluno.sync.directory import directory_page
from beluno.sync.pull import pull_scope
from beluno.sync.push import push_batch
from beluno.sync.scopes import ScopeKey

router = APIRouter(prefix="/v1/sync", tags=["sync"])
PROJECTOR = FeedProjector()

HANDSHAKE_ERRORS = problem_responses(401, 422, 426, 503)
PULL_ERRORS = problem_responses(401, 422, 426, 503)
PUSH_ERRORS = problem_responses(401, 413, 422, 426, 429, 503)


def require_protocol_version(version: int) -> None:
    if not PROTOCOL_VERSION_MIN <= version <= PROTOCOL_VERSION_MAX:
        raise client_upgrade_required(
            f"sync protocol version {version} is outside "
            f"{PROTOCOL_VERSION_MIN}..{PROTOCOL_VERSION_MAX}"
        )


def protocol_info() -> ProtocolInfo:
    return ProtocolInfo(
        version=PROTOCOL_VERSION_MAX,
        min_version=PROTOCOL_VERSION_MIN,
        max_version=PROTOCOL_VERSION_MAX,
    )


def command_catalog(registry: CommandRegistry) -> list[CommandInfo]:
    return [
        CommandInfo(
            name=command.name,
            schema_version=command.schema_version,
            versioned=command.versioned,
            target_fields=list(command.target_fields),
        )
        for command in registry.catalog()
    ]


def cursor_codec(runtime: Runtime) -> CursorCodec:
    return CursorCodec(runtime.require_hasher())


@router.post("/handshake", response_model=HandshakeResponse, responses=HANDSHAKE_ERRORS)
async def handshake(
    body: HandshakeRequest, runtime: RuntimeDep, runner: RunnerDep, actor: ActorDep
) -> HandshakeResponse:
    """Negotiate the protocol and list the scopes the caller may sync right now.

    A scope missing from the directory has been revoked: purge it locally.
    """

    require_protocol_version(body.protocol_version)
    settings = runtime.settings
    after = None
    if body.directory_cursor is not None:
        try:
            after = decode_directory_cursor(body.directory_cursor)
        except CursorError as error:
            raise validation_error("directory_cursor is invalid") from error
    async with open_context(runtime, actor) as ctx:
        entries, next_scope = await directory_page(ctx, after=after)
    return HandshakeResponse(
        protocol=protocol_info(),
        features=SyncFeatures(
            push_enabled=settings.sync_push_enabled,
            pull_enabled=settings.sync_pull_enabled,
            disabled_commands=sorted(settings.sync_disabled_commands),
        ),
        limits=SyncLimits(
            push_max_operations=settings.sync_push_max_operations,
            push_max_bytes=settings.sync_push_max_bytes,
            pull_max_scopes=MAX_PULL_SCOPES,
            pull_page_size=settings.sync_pull_page_size,
            pull_max_page_size=MAX_PULL_PAGE_SIZE,
        ),
        retention=RetentionInfo(
            offline_window_days=settings.sync_offline_window_days,
            change_retention_days=settings.sync_change_retention_days,
            operation_retention_days=settings.sync_operation_retention_days,
        ),
        commands=command_catalog(runner.registry),
        scopes=[
            ScopeEntry(
                scope=str(entry.scope),
                head=entry.head,
                floor=entry.floor,
                generation=entry.generation,
                access=entry.level.value,
            )
            for entry in entries
        ],
        next_directory_cursor=encode_directory_cursor(next_scope) if next_scope else None,
    )


@router.post("/pull", response_model=PullResponse, responses=PULL_ERRORS)
async def pull(body: PullRequest, runtime: RuntimeDep, actor: ActorDep) -> PullResponse:
    """Fetch one page per scope: a snapshot page while bootstrapping, then changes.

    Apply a page and store its cursor in one local transaction; keep the old
    cursor if applying fails. ``resync_required`` means the cursor predates the
    retention floor, a restore, or an access change: pull again with a null cursor.
    """

    require_protocol_version(body.protocol_version)
    if not runtime.settings.sync_pull_enabled:
        raise feature_disabled()
    scopes = [ScopeKey.parse(item.scope) for item in body.scopes]
    if len(set(scopes)) != len(scopes):
        raise validation_error("scopes must not repeat")
    page_size = body.page_size or runtime.settings.sync_pull_page_size
    codec = cursor_codec(runtime)
    pages = []
    async with open_context(runtime, actor) as ctx:
        for scope, item in zip(scopes, body.scopes, strict=True):
            pages.append(
                await pull_scope(
                    ctx,
                    codec=codec,
                    projector=PROJECTOR,
                    scope=scope,
                    raw_cursor=item.cursor,
                    page_size=page_size,
                )
            )
    return PullResponse(
        scopes=[
            PullScopeResponse(
                scope=str(page.scope),
                status=page.status,
                cursor=page.cursor,
                has_more=page.has_more,
                head=page.head,
                changes=[ChangeItem.model_validate(item.__dict__) for item in page.items],
            )
            for page in pages
        ]
    )


def request_too_large() -> BelunoError:
    return BelunoError(
        status=413,
        code="REQUEST_TOO_LARGE",
        title="Push batch too large",
        detail="Split the batch; see limits.push_max_bytes from the handshake",
    )


@router.post("/push", response_model=PushResponse, responses=PUSH_ERRORS)
async def push(
    body: PushRequest, request: Request, runtime: RuntimeDep, runner: RunnerDep, actor: ActorDep
) -> PushResponse:
    """Apply queued operations in order; every operation gets its own result.

    Keep retrying ``retry`` and ``skipped`` operations unchanged with the same
    ``operation_id``; drop or revise ``rejected``, ``conflict`` and
    ``upgrade_required`` ones. Replays of applied operations are free.
    """

    require_protocol_version(body.protocol_version)
    settings = runtime.settings
    if not settings.sync_push_enabled:
        raise feature_disabled()
    declared_length = request.headers.get("content-length")
    if declared_length is not None and int(declared_length) > settings.sync_push_max_bytes:
        raise request_too_large()
    if len(body.operations) > settings.sync_push_max_operations:
        raise validation_error(
            f"a batch may contain at most {settings.sync_push_max_operations} operations"
        )
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.SYNC_PUSH_PER_USER, str(actor.user_id)
    )
    return PushResponse(results=await push_batch(runner, actor, body))
