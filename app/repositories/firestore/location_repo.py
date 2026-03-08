"""Firestore-backed LocationRepository."""

from __future__ import annotations


from google.cloud.firestore_v1.async_client import AsyncClient

from app.models.location import Location
from app.repositories.firestore._base import (
    FirestoreBaseRepository,
    doc_to_dict,
    new_id,
    utc_now,
)


def _location_to_doc(loc: Location) -> dict:
    return {
        "canonical_key": loc.canonical_key,
        "display_name": loc.display_name,
        "city": loc.city,
        "region": loc.region,
        "country_code": loc.country_code,
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "geonames_id": loc.geonames_id,
        "source_data": loc.source_data,
        "created_at": loc.created_at,
        "updated_at": loc.updated_at,
    }


def _doc_to_location(data: dict) -> Location:
    return Location(
        id=data["id"],
        canonical_key=data.get("canonical_key", ""),
        display_name=data.get("display_name", ""),
        city=data.get("city"),
        region=data.get("region"),
        country_code=data.get("country_code"),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        geonames_id=data.get("geonames_id"),
        source_data=data.get("source_data"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


class FirestoreLocationRepository(FirestoreBaseRepository):
    def __init__(self, db: AsyncClient):
        super().__init__(db, "locations")

    async def get_by_id(self, location_id: str) -> Location | None:
        doc = await self.collection.document(location_id).get()
        data = doc_to_dict(doc)
        if data is None:
            return None
        return _doc_to_location(data)

    async def get_by_canonical_key(self, canonical_key: str) -> Location | None:
        query = self.collection.where("canonical_key", "==", canonical_key).limit(1)
        async for doc in query.stream():
            data = doc_to_dict(doc)
            if data:
                return _doc_to_location(data)
        return None

    async def upsert(self, location: Location) -> Location:
        existing = await self.get_by_canonical_key(location.canonical_key)
        if existing:
            return existing

        if not location.id:
            location.id = new_id()
        now = utc_now()
        if not location.created_at:
            location.created_at = now
        if not location.updated_at:
            location.updated_at = now

        await self.collection.document(location.id).set(_location_to_doc(location))
        return location
