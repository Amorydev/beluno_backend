"""Lazily built worker-process runtime shared by every job handler."""

from __future__ import annotations

from beluno.auth import AccessTokenCodec
from beluno.config import get_settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.email_delivery import EmailSender, build_email_sender
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.observability.setup import configure_observability
from beluno.push import PushSender, build_push_sender
from beluno.token_hashing import TokenHasher

_runtime: Runtime | None = None
_email_sender: EmailSender | None = None
_push_sender: PushSender | None = None


def get_worker_runtime() -> Runtime:
    """One pool per worker process, connected with the worker database role."""

    global _runtime
    if _runtime is None:
        settings = get_settings()
        configure_observability(None, settings)
        _runtime = Runtime(
            settings=settings,
            database=Database.for_worker(settings),
            tokens=AccessTokenCodec(settings),
            hasher=TokenHasher.from_settings(settings),
            identity_verifier=ExternalIdentityVerifier(settings),
        )
    return _runtime


def get_email_sender() -> EmailSender:
    global _email_sender
    if _email_sender is None:
        _email_sender = build_email_sender(get_worker_runtime().settings)
    return _email_sender


def get_push_sender() -> PushSender:
    global _push_sender
    if _push_sender is None:
        _push_sender = build_push_sender(get_worker_runtime().settings)
    return _push_sender
