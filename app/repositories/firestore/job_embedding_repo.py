"""Firestore-backed JobEmbeddingRepository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.vector import Vector

from app.repositories.firestore._base import (
    FirestoreBaseRepository,
    doc_to_dict,
    new_id,
    utc_now,
)


@dataclass(frozen=True)
class JobEmbeddingUpsertPayload:
    job_id: str
    embedding: list[float]
    content_fingerprint: str | None


def _embedding_to_doc(
    job_id: str,
    embedding: list[float],
    content_fingerprint: str | None,
    embedding_kind: str,
    embedding_target_revision: int,
    embedding_model: str,
    embedding_dim: int,
    created_at: datetime,
    updated_at: datetime,
) -> dict:
    return {
        "job_id": job_id,
        "embedding_kind": embedding_kind,
        "embedding_target_revision": embedding_target_revision,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "embedding": Vector(embedding),
        "content_fingerprint": content_fingerprint,
        "created_at": created_at,
        "updated_at": updated_at,
    }


class FirestoreJobEmbeddingRepository(FirestoreBaseRepository):
    def __init__(self, db: AsyncClient):
        super().__init__(db, "job_embeddings")

    def _target_query(
        self,
        *,
        job_id: str,
        embedding_kind: str,
        embedding_target_revision: int,
        embedding_model: str,
        embedding_dim: int,
    ):
        return (
            self.collection
            .where("job_id", "==", job_id)
            .where("embedding_kind", "==", embedding_kind)
            .where("embedding_target_revision", "==", embedding_target_revision)
            .where("embedding_model", "==", embedding_model)
            .where("embedding_dim", "==", embedding_dim)
            .limit(1)
        )

    async def get_by_job_and_target(
        self,
        *,
        job_id: str,
        embedding_kind: str,
        embedding_target_revision: int,
        embedding_model: str,
        embedding_dim: int,
    ) -> dict | None:
        query = self._target_query(
            job_id=job_id,
            embedding_kind=embedding_kind,
            embedding_target_revision=embedding_target_revision,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        )
        async for doc in query.stream():
            return doc_to_dict(doc)
        return None

    async def list_by_job_ids_and_target(
        self,
        *,
        job_ids: Sequence[str],
        embedding_kind: str,
        embedding_target_revision: int,
        embedding_model: str,
        embedding_dim: int,
    ) -> dict[str, dict]:
        ids = [jid for jid in job_ids if jid]
        if not ids:
            return {}

        result: dict[str, dict] = {}
        for i in range(0, len(ids), 30):
            chunk = ids[i : i + 30]
            query = (
                self.collection
                .where("job_id", "in", chunk)
                .where("embedding_kind", "==", embedding_kind)
                .where("embedding_target_revision", "==", embedding_target_revision)
                .where("embedding_model", "==", embedding_model)
                .where("embedding_dim", "==", embedding_dim)
            )
            async for doc in query.stream():
                data = doc_to_dict(doc)
                if data:
                    result[data["job_id"]] = data
        return result

    async def list_fresh_job_ids_for_target(
        self,
        *,
        job_content_fingerprints: dict[str, str | None],
        embedding_kind: str,
        embedding_target_revision: int,
        embedding_model: str,
        embedding_dim: int,
    ) -> set[str]:
        if not job_content_fingerprints:
            return set()

        rows_by_job_id = await self.list_by_job_ids_and_target(
            job_ids=list(job_content_fingerprints),
            embedding_kind=embedding_kind,
            embedding_target_revision=embedding_target_revision,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        )

        fresh_ids: set[str] = set()
        for job_id, fingerprint in job_content_fingerprints.items():
            row = rows_by_job_id.get(job_id)
            if row and row.get("content_fingerprint") == fingerprint:
                fresh_ids.add(job_id)
        return fresh_ids

    async def upsert_for_target(
        self,
        *,
        job_id: str,
        embedding: list[float],
        content_fingerprint: str | None,
        embedding_kind: str,
        embedding_target_revision: int,
        embedding_model: str,
        embedding_dim: int,
        updated_at: datetime | None = None,
    ) -> dict:
        existing = await self.get_by_job_and_target(
            job_id=job_id,
            embedding_kind=embedding_kind,
            embedding_target_revision=embedding_target_revision,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        )
        now = updated_at or utc_now()

        if existing:
            doc_id = existing["id"]
            await self.collection.document(doc_id).update({
                "embedding": Vector(embedding),
                "content_fingerprint": content_fingerprint,
                "updated_at": now,
            })
            existing["embedding"] = embedding
            existing["content_fingerprint"] = content_fingerprint
            existing["updated_at"] = now
        else:
            doc_id = new_id()
            doc_data = _embedding_to_doc(
                job_id=job_id,
                embedding=embedding,
                content_fingerprint=content_fingerprint,
                embedding_kind=embedding_kind,
                embedding_target_revision=embedding_target_revision,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                created_at=now,
                updated_at=now,
            )
            await self.collection.document(doc_id).set(doc_data)
            doc_data["id"] = doc_id
            existing = doc_data

        # Also write the vector to the job document for find_nearest queries
        jobs_ref = self._db.collection("jobs").document(job_id)
        await jobs_ref.update({
            "embedding": Vector(embedding),
            "embedding_model": embedding_model,
            "embedding_updated_at": now,
        })

        return existing

    async def upsert_many_for_target(
        self,
        *,
        rows: Sequence[JobEmbeddingUpsertPayload],
        embedding_kind: str,
        embedding_target_revision: int,
        embedding_model: str,
        embedding_dim: int,
        updated_at: datetime | None = None,
    ) -> int:
        if not rows:
            return 0

        # Dedupe: last-wins per job_id
        deduped: list[JobEmbeddingUpsertPayload] = []
        seen: set[str] = set()
        for row in reversed(rows):
            if not row.job_id or row.job_id in seen:
                continue
            seen.add(row.job_id)
            deduped.append(row)
        deduped.reverse()

        now = updated_at or utc_now()
        for payload in deduped:
            await self.upsert_for_target(
                job_id=payload.job_id,
                embedding=payload.embedding,
                content_fingerprint=payload.content_fingerprint,
                embedding_kind=embedding_kind,
                embedding_target_revision=embedding_target_revision,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                updated_at=now,
            )
        return len(deduped)
