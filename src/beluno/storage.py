"""S3-compatible object storage (RustFS self-hosted) for media.

Two clients: one signs URLs for the apps (``storage_public_url``, the address they
reach), the other moves bytes for the worker (``storage_endpoint_url``). Signing is
local computation; only the worker's calls touch the network, and they run in a
thread so the event loop keeps serving. Keys are random IDs and never carry
personal data: ``incoming/{id}`` until scanned, ``media/{id}`` once clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from beluno.config import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

UPLOAD_URL_TTL = timedelta(minutes=15)
DOWNLOAD_URL_TTL = timedelta(minutes=5)


class StorageNotConfigured(RuntimeError):
    """Media needs BELUNO_STORAGE_ENDPOINT_URL and its access keys."""


class ObjectTooLarge(ValueError):
    """The uploaded object is larger than the upload declared."""


def incoming_key(media_id: UUID) -> str:
    return f"incoming/{media_id}"


def ready_key(media_id: UUID) -> str:
    return f"media/{media_id}"


def archive_key(archive_id: UUID) -> str:
    return f"archives/{archive_id}.zip"


@dataclass(frozen=True)
class ObjectStorage:
    bucket: str
    signer: S3Client
    mover: S3Client

    @classmethod
    def from_settings(cls, settings: Settings) -> ObjectStorage:
        endpoint = settings.storage_endpoint_url
        key_id, secret = settings.storage_access_key_id, settings.storage_secret_access_key
        if not endpoint or key_id is None or secret is None:
            raise StorageNotConfigured("media storage is not configured")

        def client(url: str) -> S3Client:
            return boto3.client(
                "s3",
                endpoint_url=url,
                region_name=settings.storage_region,
                aws_access_key_id=key_id.get_secret_value(),
                aws_secret_access_key=secret.get_secret_value(),
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=30,
                ),
            )

        return cls(
            bucket=settings.storage_bucket,
            signer=client(settings.storage_public_url or endpoint),
            mover=client(endpoint),
        )

    async def ensure_bucket(self) -> None:
        """Create the media bucket if it is missing (worker start; idempotent)."""

        def ensure() -> None:
            try:
                self.mover.head_bucket(Bucket=self.bucket)
            except ClientError:
                self.mover.create_bucket(Bucket=self.bucket)

        await anyio.to_thread.run_sync(ensure)

    # --- signing (no network) --------------------------------------------------------

    def upload_url(self, key: str, content_type: str, size: int) -> str:
        """A PUT URL bound to this type and exact size: storage rejects anything else."""

        return self.signer.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": size,
            },
            ExpiresIn=int(UPLOAD_URL_TTL.total_seconds()),
        )

    def download_url(
        self, key: str, content_type: str, filename: str, *, attachment: bool = False
    ) -> str:
        disposition = "attachment" if attachment else "inline"
        return self.signer.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
                "ResponseCacheControl": "private, max-age=300",
            },
            ExpiresIn=int(DOWNLOAD_URL_TTL.total_seconds()),
        )

    # --- worker I/O (network, in a thread) --------------------------------------------

    async def read(self, key: str, max_bytes: int) -> bytes | None:
        """The object's bytes, or ``None`` when it is missing; refuses oversize objects."""

        def fetch() -> bytes | None:
            try:
                found = self.mover.get_object(Bucket=self.bucket, Key=key)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    return None
                raise
            body = found["Body"]
            try:
                data = body.read(max_bytes + 1)
            finally:
                body.close()
            if len(data) > max_bytes:
                raise ObjectTooLarge(key)
            return data

        return await anyio.to_thread.run_sync(fetch)

    async def write(self, key: str, data: bytes, content_type: str) -> None:
        def put() -> None:
            self.mover.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

        await anyio.to_thread.run_sync(put)

    async def write_file(self, key: str, path: Path, content_type: str) -> None:
        """Upload a file from disk (multipart for large ones), never whole in memory."""

        def put() -> None:
            self.mover.upload_file(
                str(path), self.bucket, key, ExtraArgs={"ContentType": content_type}
            )

        await anyio.to_thread.run_sync(put)

    async def delete(self, key: str) -> None:
        """Deleting a missing object succeeds (S3 semantics)."""

        def remove() -> None:
            self.mover.delete_object(Bucket=self.bucket, Key=key)

        await anyio.to_thread.run_sync(remove)
