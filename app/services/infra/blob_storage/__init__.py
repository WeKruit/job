"""Blob storage helpers for large job fields.

Storage uploads happen before the database commit so the database never points
at a missing object. If the later database transaction rolls back, the uploaded
blob becomes an orphan. That tradeoff is acceptable because orphaned blobs are
repairable, while a database pointer to a missing blob would break reads.
"""

from __future__ import annotations

import httpx

from app.core.config import Settings, get_settings

from .builder import (
    PreparedBlob,
    build_description_html_blob,
    build_raw_payload_blob,
    compute_sha256_hex,
)
from .client import (
    BlobNotFoundError,
    BlobStorageClient,
    BlobStorageError,
    BlobStorageNotConfiguredError,
    DisabledBlobStorage,
    NoOpBlobStorage,
)
from .supabase import SupabaseBlobStorage


def create_blob_storage(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BlobStorageClient:
    """Create the configured blob storage backend."""
    settings = settings or get_settings()
    provider = settings.storage_provider.strip().lower()

    if provider in {"", "none", "disabled"}:
        return DisabledBlobStorage(
            reason="Blob storage is disabled. Set STORAGE_PROVIDER=supabase to enable it.",
        )

    if provider != "supabase":
        return DisabledBlobStorage(
            reason=f"Unsupported storage provider: {settings.storage_provider}",
        )

    missing = []
    if not settings.supabase_storage_base_url:
        missing.append("SUPABASE_STORAGE_BASE_URL")
    if not settings.supabase_storage_bucket:
        missing.append("SUPABASE_STORAGE_BUCKET")
    if not settings.supabase_storage_service_key:
        missing.append("SUPABASE_STORAGE_SERVICE_KEY")
    if missing:
        return DisabledBlobStorage(
            reason="Supabase Storage is not fully configured. Missing: " + ", ".join(missing),
        )

    return SupabaseBlobStorage(
        base_url=settings.supabase_storage_base_url,
        bucket=settings.supabase_storage_bucket,
        service_key=settings.supabase_storage_service_key,
        timeout_seconds=settings.storage_timeout_seconds,
        transport=transport,
    )


__all__ = [
    "BlobNotFoundError",
    "BlobStorageClient",
    "BlobStorageError",
    "BlobStorageNotConfiguredError",
    "DisabledBlobStorage",
    "NoOpBlobStorage",
    "PreparedBlob",
    "SupabaseBlobStorage",
    "build_description_html_blob",
    "build_raw_payload_blob",
    "compute_sha256_hex",
    "create_blob_storage",
]
