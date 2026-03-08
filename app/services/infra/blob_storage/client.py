from __future__ import annotations

from typing import Protocol


class BlobStorageError(RuntimeError):
    """Base exception for blob storage failures."""


class BlobStorageNotConfiguredError(BlobStorageError):
    """Raised when blob storage is required but not configured."""


class BlobNotFoundError(BlobStorageError):
    """Raised when a configured blob key cannot be fetched."""


class BlobStorageClient(Protocol):
    """Interface for blob storage backends."""

    @property
    def is_enabled(self) -> bool:
        """Whether the backend is usable for read/write calls."""

    async def upload_if_missing(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        content_encoding: str = "gzip",
    ) -> bool:
        """Upload a blob if the object key does not already exist."""

    async def download(self, *, key: str) -> bytes:
        """Download the stored bytes for one object key."""


class DisabledBlobStorage:
    """Placeholder backend that explains why storage is unavailable."""

    def __init__(self, *, reason: str):
        self.reason = reason

    @property
    def is_enabled(self) -> bool:
        return False

    async def upload_if_missing(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        content_encoding: str = "gzip",
    ) -> bool:
        _ = (key, data, content_type, content_encoding)
        raise BlobStorageNotConfiguredError(self.reason)

    async def download(self, *, key: str) -> bytes:
        _ = key
        raise BlobStorageNotConfiguredError(self.reason)


class NoOpBlobStorage:
    """Backend that silently skips all blob operations.

    Used by the Firestore path where Supabase blob storage is unavailable.
    Jobs are saved without blob pointers; description_html and raw_payload
    are not persisted externally.
    """

    @property
    def is_enabled(self) -> bool:
        return True

    async def upload_if_missing(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        content_encoding: str = "gzip",
    ) -> bool:
        return False

    async def download(self, *, key: str) -> bytes:
        raise BlobNotFoundError(f"NoOpBlobStorage does not store blobs: {key}")
