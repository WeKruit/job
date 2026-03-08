"""Shared helpers for Firestore repositories."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from google.cloud.firestore_v1.async_client import AsyncClient


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def _serialize_datetime(value: Any) -> Any:
    """Ensure datetime values are timezone-aware for Firestore."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return value


def doc_to_dict(doc_snapshot) -> dict[str, Any] | None:
    """Convert a Firestore document snapshot to a dict with id included."""
    if not doc_snapshot.exists:
        return None
    data = doc_snapshot.to_dict()
    data["id"] = doc_snapshot.id
    return data


class FirestoreBaseRepository:
    """Base class providing Firestore client and collection reference."""

    def __init__(self, db: AsyncClient, collection_name: str):
        self._db = db
        self._collection_name = collection_name

    @property
    def collection(self):
        return self._db.collection(self._collection_name)
