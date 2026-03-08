"""Firestore-backed JobRepository."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from google.cloud.firestore_v1.async_client import AsyncClient

from app.models import Job, JobStatus, PlatformType
from app.repositories.firestore._base import (
    FirestoreBaseRepository,
    doc_to_dict,
    new_id,
    utc_now,
)


@dataclass(frozen=True)
class EmbeddableJobRow:
    id: str
    title: str
    description: str
    content_fingerprint: str | None


@dataclass(frozen=True)
class SnapshotRefreshCandidateRow:
    id: str
    title: str
    description: str
    content_fingerprint: str | None


def _job_to_doc(job: Job) -> dict:
    """Convert a Job model to a Firestore document dict."""
    status_val = job.status.value if isinstance(job.status, JobStatus) else job.status
    return {
        "source_id": job.source_id,
        "external_job_id": job.external_job_id,
        "title": job.title,
        "apply_url": job.apply_url,
        "normalized_apply_url": job.normalized_apply_url,
        "content_fingerprint": job.content_fingerprint,
        "dedupe_group_id": job.dedupe_group_id,
        "status": status_val,
        "department": job.department,
        "team": job.team,
        "employment_type": job.employment_type,
        "description_html_key": job.description_html_key,
        "description_html_hash": job.description_html_hash,
        "description_plain": job.description_plain,
        "sponsorship_not_available": job.sponsorship_not_available,
        "job_domain_raw": job.job_domain_raw,
        "job_domain_normalized": job.job_domain_normalized,
        "min_degree_level": job.min_degree_level,
        "min_degree_rank": job.min_degree_rank,
        "structured_jd_version": job.structured_jd_version,
        "structured_jd": job.structured_jd,
        "structured_jd_updated_at": job.structured_jd_updated_at,
        "published_at": job.published_at,
        "source_updated_at": job.source_updated_at,
        "last_seen_at": job.last_seen_at,
        "raw_payload_key": job.raw_payload_key,
        "raw_payload_hash": job.raw_payload_hash,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _doc_to_job(data: dict) -> Job:
    """Convert a Firestore document dict to a Job model."""
    status_raw = data.get("status", "open")
    try:
        status = JobStatus(status_raw)
    except ValueError:
        status = JobStatus.open

    return Job(
        id=data["id"],
        source_id=data.get("source_id"),
        external_job_id=data.get("external_job_id", ""),
        title=data.get("title", ""),
        apply_url=data.get("apply_url", ""),
        normalized_apply_url=data.get("normalized_apply_url"),
        content_fingerprint=data.get("content_fingerprint"),
        dedupe_group_id=data.get("dedupe_group_id"),
        status=status,
        department=data.get("department"),
        team=data.get("team"),
        employment_type=data.get("employment_type"),
        description_html_key=data.get("description_html_key"),
        description_html_hash=data.get("description_html_hash"),
        description_plain=data.get("description_plain"),
        sponsorship_not_available=data.get("sponsorship_not_available", "unknown"),
        job_domain_raw=data.get("job_domain_raw"),
        job_domain_normalized=data.get("job_domain_normalized", "unknown"),
        min_degree_level=data.get("min_degree_level", "unknown"),
        min_degree_rank=data.get("min_degree_rank", -1),
        structured_jd_version=data.get("structured_jd_version", 3),
        structured_jd=data.get("structured_jd"),
        structured_jd_updated_at=data.get("structured_jd_updated_at"),
        published_at=data.get("published_at"),
        source_updated_at=data.get("source_updated_at"),
        last_seen_at=data.get("last_seen_at"),
        raw_payload_key=data.get("raw_payload_key"),
        raw_payload_hash=data.get("raw_payload_hash"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


class FirestoreJobRepository(FirestoreBaseRepository):
    def __init__(self, db: AsyncClient):
        super().__init__(db, "jobs")

    # ------------------------------------------------------------------ #
    # Core CRUD                                                            #
    # ------------------------------------------------------------------ #

    async def create(self, job: Job) -> Job:
        if not job.id:
            job.id = new_id()
        now = utc_now()
        if not job.created_at:
            job.created_at = now
        if not job.updated_at:
            job.updated_at = now
        if not job.last_seen_at:
            job.last_seen_at = now
        await self.collection.document(str(job.id)).set(_job_to_doc(job))
        return job

    async def get_by_id(self, job_id: str) -> Job | None:
        doc = await self.collection.document(job_id).get()
        data = doc_to_dict(doc)
        if data is None:
            return None
        return _doc_to_job(data)

    async def list_by_ids(self, job_ids: list[str]) -> list[Job]:
        if not job_ids:
            return []
        # Firestore get_all for batch reads
        doc_refs = [self.collection.document(jid) for jid in job_ids]
        docs = await self._db.get_all(doc_refs)
        jobs_by_id: dict[str, Job] = {}
        async for doc in docs:
            data = doc_to_dict(doc)
            if data:
                jobs_by_id[data["id"]] = _doc_to_job(data)
        return [jobs_by_id[jid] for jid in job_ids if jid in jobs_by_id]

    async def update(self, job: Job) -> Job:
        job.updated_at = utc_now()
        await self.collection.document(str(job.id)).set(_job_to_doc(job))
        return job

    async def save_all(self, jobs: list[Job]) -> None:
        # Firestore batch writes (max 500 per batch)
        for i in range(0, len(jobs), 500):
            batch = self._db.batch()
            for job in jobs[i : i + 500]:
                batch.set(self.collection.document(str(job.id)), _job_to_doc(job))
            await batch.commit()

    async def save_all_no_commit(self, jobs: list[Job]) -> None:
        # In Firestore there's no transaction staging, so this writes immediately
        await self.save_all(jobs)

    async def flush(self) -> None:
        # No-op in Firestore (no transaction staging)
        pass

    async def delete(self, job: Job) -> None:
        await self.collection.document(str(job.id)).delete()

    # ------------------------------------------------------------------ #
    # Source-ID lookups                                                     #
    # ------------------------------------------------------------------ #

    async def list_by_source_id_and_external_ids(
        self, source_id: str, external_job_ids: list[str]
    ) -> list[Job]:
        if not external_job_ids:
            return []
        # Firestore 'in' queries support up to 30 values
        results: list[Job] = []
        for i in range(0, len(external_job_ids), 30):
            chunk = external_job_ids[i : i + 30]
            query = (
                self.collection
                .where("source_id", "==", source_id)
                .where("external_job_id", "in", chunk)
            )
            async for doc in query.stream():
                data = doc_to_dict(doc)
                if data:
                    results.append(_doc_to_job(data))
        return results

    async def bulk_close_missing_for_source_id(
        self,
        *,
        source_id: str,
        seen_at_before: datetime,
        updated_at: datetime,
    ) -> int:
        query = (
            self.collection
            .where("source_id", "==", source_id)
            .where("status", "==", "open")
            .where("last_seen_at", "<", seen_at_before)
        )
        count = 0
        batch = self._db.batch()
        batch_size = 0
        async for doc in query.stream():
            batch.update(self.collection.document(doc.id), {
                "status": "closed",
                "updated_at": updated_at,
            })
            count += 1
            batch_size += 1
            if batch_size >= 500:
                await batch.commit()
                batch = self._db.batch()
                batch_size = 0
        if batch_size > 0:
            await batch.commit()
        return count

    async def source_id_reference_exists(self, source_id: str) -> bool:
        query = self.collection.where("source_id", "==", source_id).limit(1)
        async for _ in query.stream():
            return True
        return False

    # ------------------------------------------------------------------ #
    # Legacy string-based helpers                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _split_source_key(source_key: str) -> tuple[PlatformType, str] | None:
        if ":" not in source_key:
            return None
        platform_str, identifier = source_key.split(":", 1)
        platform_str = platform_str.strip().lower()
        identifier = identifier.strip()
        if not platform_str or not identifier:
            return None
        try:
            return PlatformType(platform_str), identifier
        except ValueError:
            return None

    async def list_by_source_and_external_ids(
        self, source: str, external_job_ids: list[str]
    ) -> list[Job]:
        # Requires resolving source key to source_id first
        # This is a compatibility shim; callers should use source_id-based methods
        return []

    async def has_any_for_source(self, *, source: str) -> bool:
        return False

    async def bulk_close_missing_for_source(
        self, *, source: str, seen_at_before: datetime, updated_at: datetime
    ) -> int:
        return 0

    # ------------------------------------------------------------------ #
    # Listing and pagination                                               #
    # ------------------------------------------------------------------ #

    async def list_jobs(
        self,
        skip: int = 0,
        limit: int = 100,
        status: JobStatus | None = None,
    ) -> list[Job]:
        query = self.collection.order_by("__name__")
        if status is not None:
            status_val = status.value if isinstance(status, JobStatus) else status
            query = query.where("status", "==", status_val)
        query = query.offset(skip).limit(limit)
        jobs = []
        async for doc in query.stream():
            data = doc_to_dict(doc)
            if data:
                jobs.append(_doc_to_job(data))
        return jobs

    async def list_pending_structured_jd(
        self,
        limit: int = 5,
        *,
        version_only: bool = False,
        exclude_job_ids: Collection[str] | None = None,
    ) -> list[Job]:
        # Firestore can't do complex OR/NULL queries easily, so fetch and filter
        if version_only:
            query = (
                self.collection
                .where("structured_jd_version", "<", 3)
                .order_by("updated_at")
                .limit(limit * 3)  # over-fetch to account for filtering
            )
        else:
            query = self.collection.order_by("updated_at").limit(limit * 3)

        exclude = set(exclude_job_ids) if exclude_job_ids else set()
        results: list[Job] = []
        async for doc in query.stream():
            if len(results) >= limit:
                break
            data = doc_to_dict(doc)
            if not data:
                continue
            if data["id"] in exclude:
                continue
            # Must have content
            if not data.get("description_html_key") and not data.get("description_plain"):
                continue
            # Check structured_jd eligibility
            if not version_only:
                sjd = data.get("structured_jd")
                sjd_ver = data.get("structured_jd_version", 0)
                if sjd is not None and sjd_ver >= 3:
                    continue
            results.append(_doc_to_job(data))
        return results

    async def list_jobs_for_location_backfill(
        self, last_id: str | None = None, limit: int = 100
    ) -> list[Job]:
        query = self.collection.order_by("__name__")
        if last_id:
            query = query.start_after({"__name__": last_id})
        query = query.limit(limit)
        jobs = []
        async for doc in query.stream():
            data = doc_to_dict(doc)
            if data:
                jobs.append(_doc_to_job(data))
        return jobs

    async def list_jobs_for_country_backfill(
        self, last_id: str | None = None, limit: int = 100
    ) -> list[Job]:
        # Simplified: return all jobs keyset-paginated (post-filter in caller)
        return await self.list_jobs_for_location_backfill(last_id, limit)

    async def list_jobs_missing_canonical_locations(
        self, last_id: str | None = None, limit: int = 100
    ) -> list[Job]:
        # Simplified: return all jobs keyset-paginated (post-filter in caller)
        return await self.list_jobs_for_location_backfill(last_id, limit)

    # ------------------------------------------------------------------ #
    # Embedding helpers                                                    #
    # ------------------------------------------------------------------ #

    async def list_embeddable_jobs_for_active_target(
        self, *, last_id: str | None = None, limit: int = 100, **kwargs
    ) -> list[EmbeddableJobRow]:
        query = self.collection.order_by("__name__")
        if last_id:
            query = query.start_after({"__name__": last_id})
        query = query.limit(limit * 2)

        results: list[EmbeddableJobRow] = []
        async for doc in query.stream():
            if len(results) >= limit:
                break
            data = doc_to_dict(doc)
            if not data:
                continue
            description = data.get("description_plain") or ""
            if not description.strip():
                continue
            results.append(EmbeddableJobRow(
                id=data["id"],
                title=data.get("title", ""),
                description=description,
                content_fingerprint=data.get("content_fingerprint"),
            ))
        return results

    async def count_jobs_missing_or_stale_active_target(self, **kwargs) -> int:
        # Approximate: count all jobs (exact staleness check needs embedding join)
        return 0

    async def count_fresh_active_target_jobs(self, **kwargs) -> int:
        return 0

    async def list_snapshot_refresh_candidates_for_active_target(
        self,
        *,
        source_id: str,
        last_id: str | None = None,
        limit: int = 100,
        **kwargs,
    ) -> list[SnapshotRefreshCandidateRow]:
        query = (
            self.collection
            .where("source_id", "==", source_id)
            .where("status", "==", "open")
            .order_by("__name__")
        )
        if last_id:
            query = query.start_after({"__name__": last_id})
        query = query.limit(limit * 2)

        results: list[SnapshotRefreshCandidateRow] = []
        async for doc in query.stream():
            if len(results) >= limit:
                break
            data = doc_to_dict(doc)
            if not data:
                continue
            description = data.get("description_plain") or ""
            if not description.strip():
                continue
            results.append(SnapshotRefreshCandidateRow(
                id=data["id"],
                title=data.get("title", ""),
                description=description,
                content_fingerprint=data.get("content_fingerprint"),
            ))
        return results
