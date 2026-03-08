"""Firestore-backed JobLocationRepository."""

from __future__ import annotations

from google.cloud.firestore_v1.async_client import AsyncClient

from app.models.job_location import JobLocation
from app.repositories.firestore._base import (
    FirestoreBaseRepository,
    doc_to_dict,
    new_id,
    utc_now,
)


def _jl_to_doc(jl: JobLocation) -> dict:
    return {
        "job_id": jl.job_id,
        "location_id": jl.location_id,
        "is_primary": jl.is_primary,
        "source_raw": jl.source_raw,
        "workplace_type": jl.workplace_type,
        "remote_scope": jl.remote_scope,
        "created_at": jl.created_at,
    }


def _doc_to_jl(data: dict) -> JobLocation:
    return JobLocation(
        id=data["id"],
        job_id=data.get("job_id", ""),
        location_id=data.get("location_id", ""),
        is_primary=data.get("is_primary", False),
        source_raw=data.get("source_raw"),
        workplace_type=data.get("workplace_type", "unknown"),
        remote_scope=data.get("remote_scope"),
        created_at=data.get("created_at"),
    )


class FirestoreJobLocationRepository(FirestoreBaseRepository):
    def __init__(self, db: AsyncClient):
        super().__init__(db, "job_locations")

    async def list_by_job_id(self, job_id: str) -> list[JobLocation]:
        query = self.collection.where("job_id", "==", job_id)
        results = []
        async for doc in query.stream():
            data = doc_to_dict(doc)
            if data:
                results.append(_doc_to_jl(data))
        return results

    async def link(
        self,
        job_id: str,
        location_id: str,
        is_primary: bool = False,
        source_raw: str | None = None,
        workplace_type: str = "unknown",
        remote_scope: str | None = None,
    ) -> JobLocation:
        links = await self.list_by_job_id(job_id)

        # Maintain one-primary-per-job invariant
        if is_primary:
            batch = self._db.batch()
            for link in links:
                if link.is_primary and link.location_id != location_id:
                    link.is_primary = False
                    batch.set(self.collection.document(link.id), _jl_to_doc(link))
            await batch.commit()

        existing = next((lk for lk in links if lk.location_id == location_id), None)
        if existing:
            if (
                existing.is_primary != is_primary
                or existing.source_raw != source_raw
                or existing.workplace_type != workplace_type
                or existing.remote_scope != remote_scope
            ):
                existing.is_primary = is_primary
                existing.source_raw = source_raw
                existing.workplace_type = workplace_type
                existing.remote_scope = remote_scope
                await self.collection.document(existing.id).set(_jl_to_doc(existing))
            return existing

        jl = JobLocation(
            id=new_id(),
            job_id=job_id,
            location_id=location_id,
            is_primary=is_primary,
            source_raw=source_raw,
            workplace_type=workplace_type,
            remote_scope=remote_scope,
            created_at=utc_now(),
        )
        await self.collection.document(jl.id).set(_jl_to_doc(jl))
        return jl

    async def unlink(self, job_id: str, location_id: str) -> None:
        query = (
            self.collection
            .where("job_id", "==", job_id)
            .where("location_id", "==", location_id)
        )
        async for doc in query.stream():
            await self.collection.document(doc.id).delete()

    async def set_primary(self, job_id: str, location_id: str) -> None:
        links = await self.list_by_job_id(job_id)
        batch = self._db.batch()
        for link in links:
            if link.is_primary and link.location_id != location_id:
                link.is_primary = False
                batch.set(self.collection.document(link.id), _jl_to_doc(link))
            elif link.location_id == location_id and not link.is_primary:
                link.is_primary = True
                batch.set(self.collection.document(link.id), _jl_to_doc(link))
        await batch.commit()
