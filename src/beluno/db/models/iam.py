"""Identity tables: application users, linked identities, sessions, and challenges."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ARRAY, BigInteger, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "iam"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None]
    locale: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    default_currency: Mapped[str | None] = mapped_column(Text)
    merged_into_user_id: Mapped[UUID | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = {"schema": "iam"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    provider: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    last_used_at: Mapped[datetime]


class AuthSession(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "iam"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    auth_method: Mapped[str] = mapped_column(Text)
    authenticated_at: Mapped[datetime]
    client_device_id: Mapped[str | None] = mapped_column(Text)
    device_label: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    app_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    idle_expires_at: Mapped[datetime]
    absolute_expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = {"schema": "iam"}  # noqa: RUF012

    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    session_id: Mapped[UUID]
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    replaced_by_hash: Mapped[bytes | None] = mapped_column(LargeBinary)


class EmailChallenge(Base):
    __tablename__ = "email_challenges"
    __table_args__ = {"schema": "iam"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    code_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    link_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    delivery_state: Mapped[str] = mapped_column(Text)
    delivered_at: Mapped[datetime | None]
    failed_attempts: Mapped[int]
    max_attempts: Mapped[int]
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class Passkey(Base):
    __tablename__ = "passkeys"
    __table_args__ = {"schema": "iam"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    credential_id: Mapped[bytes] = mapped_column(LargeBinary)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger)
    transports: Mapped[list[str]] = mapped_column(ARRAY(Text))
    label: Mapped[str] = mapped_column(Text)
    backed_up: Mapped[bool]
    created_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]


class WebAuthnChallenge(Base):
    __tablename__ = "webauthn_challenges"
    __table_args__ = {"schema": "iam"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(Text)
    user_id: Mapped[UUID | None]
    challenge_hash: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
