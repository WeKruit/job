"""FastAPI dependency injection providers.

Provides either SQL or Firestore-backed repositories depending on config.
When FIRESTORE_CREDENTIALS_FILE is set, all repositories use Firestore.
Otherwise, the legacy SQL path is used.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.services.application.job_service import JobService
from app.services.application.source_service import SourceService


def get_job_service() -> JobService:
    """Return JobService backed by Firestore or SQL depending on config."""
    settings = get_settings()
    if settings.firestore_credentials_file:
        from app.infrastructure.firestore_client import get_firestore_client
        from app.repositories.firestore import FirestoreJobRepository, FirestoreSourceRepository

        db = get_firestore_client()
        return JobService(FirestoreJobRepository(db), source_repository=FirestoreSourceRepository(db))

    raise NotImplementedError(
        "SQL-backed JobService requires an async session via Depends(get_session). "
        "Use the inline dependency in the route handler or set FIRESTORE_CREDENTIALS_FILE."
    )


def get_source_service() -> SourceService:
    """Return SourceService backed by Firestore or SQL depending on config."""
    settings = get_settings()
    if settings.firestore_credentials_file:
        from app.infrastructure.firestore_client import get_firestore_client
        from app.repositories.firestore import (
            FirestoreJobRepository,
            FirestoreSourceRepository,
            FirestoreSyncRunRepository,
        )

        db = get_firestore_client()
        return SourceService(
            FirestoreSourceRepository(db),
            FirestoreSyncRunRepository(db),
            FirestoreJobRepository(db),
        )

    raise NotImplementedError(
        "SQL-backed SourceService requires an async session via Depends(get_session). "
        "Use the inline dependency in the route handler or set FIRESTORE_CREDENTIALS_FILE."
    )
