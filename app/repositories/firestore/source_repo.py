"""Firestore-backed SourceRepository."""

from __future__ import annotations

from datetime import datetime, timezone

from google.cloud.firestore_v1.async_client import AsyncClient

from app.models import PlatformType, Source
from app.repositories.firestore._base import (
    FirestoreBaseRepository,
    doc_to_dict,
    new_id,
    utc_now,
)


def _source_to_doc(source: Source) -> dict:
    """Convert a Source model to a Firestore document dict."""
    return {
        "name": source.name,
        "name_normalized": source.name_normalized,
        "platform": source.platform.value if isinstance(source.platform, PlatformType) else source.platform,
        "identifier": source.identifier,
        "enabled": source.enabled,
        "notes": source.notes,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


def _doc_to_source(data: dict) -> Source:
    """Convert a Firestore document dict to a Source model."""
    return Source(
        id=data["id"],
        name=data.get("name", ""),
        name_normalized=data.get("name_normalized", ""),
        platform=PlatformType(data["platform"]),
        identifier=data.get("identifier", ""),
        enabled=data.get("enabled", True),
        notes=data.get("notes"),
        created_at=data.get("created_at", datetime.now(timezone.utc)),
        updated_at=data.get("updated_at", datetime.now(timezone.utc)),
    )


class FirestoreSourceRepository(FirestoreBaseRepository):
    def __init__(self, db: AsyncClient):
        super().__init__(db, "sources")

    async def create(self, source: Source) -> Source:
        now = utc_now()
        if not source.id:
            source.id = new_id()
        source.created_at = now
        source.updated_at = now
        await self.collection.document(source.id).set(_source_to_doc(source))
        return source

    async def get_by_id(self, source_id: str) -> Source | None:
        doc = await self.collection.document(source_id).get()
        data = doc_to_dict(doc)
        if data is None:
            return None
        return _doc_to_source(data)

    async def get_by_name_and_platform(
        self, name_normalized: str, platform: PlatformType
    ) -> Source | None:
        normalized = name_normalized.strip().lower()
        platform_val = platform.value if isinstance(platform, PlatformType) else platform
        query = (
            self.collection
            .where("name_normalized", "==", normalized)
            .where("platform", "==", platform_val)
            .limit(1)
        )
        docs = []
        async for doc in query.stream():
            docs.append(doc)
        if not docs:
            return None
        data = doc_to_dict(docs[0])
        return _doc_to_source(data)

    async def get_by_platform_and_identifier(
        self, platform: PlatformType, identifier: str
    ) -> Source | None:
        platform_val = platform.value if isinstance(platform, PlatformType) else platform
        query = (
            self.collection
            .where("platform", "==", platform_val)
            .where("identifier", "==", identifier.strip())
            .limit(1)
        )
        docs = []
        async for doc in query.stream():
            docs.append(doc)
        if not docs:
            return None
        data = doc_to_dict(docs[0])
        return _doc_to_source(data)

    async def get_by_source_key(self, source_key: str) -> Source | None:
        if ":" not in source_key:
            return None
        platform_str, identifier = source_key.split(":", 1)
        try:
            platform = PlatformType(platform_str.strip())
        except ValueError:
            return None
        return await self.get_by_platform_and_identifier(platform, identifier)

    async def get_by_name_normalized(self, name_normalized: str) -> Source | None:
        normalized = name_normalized.strip().lower()
        query = self.collection.where("name_normalized", "==", normalized).limit(1)
        docs = []
        async for doc in query.stream():
            docs.append(doc)
        if not docs:
            return None
        data = doc_to_dict(docs[0])
        return _doc_to_source(data)

    async def update(self, source: Source) -> Source:
        source.updated_at = utc_now()
        await self.collection.document(source.id).set(_source_to_doc(source))
        return source

    async def delete(self, source: Source) -> None:
        await self.collection.document(source.id).delete()

    async def list(
        self,
        enabled: bool | None = None,
        platform: PlatformType | None = None,
    ) -> list[Source]:
        query = self.collection
        if enabled is not None:
            query = query.where("enabled", "==", enabled)
        if platform is not None:
            platform_val = platform.value if isinstance(platform, PlatformType) else platform
            query = query.where("platform", "==", platform_val)
        sources = []
        async for doc in query.stream():
            data = doc_to_dict(doc)
            if data:
                sources.append(_doc_to_source(data))
        return sources
