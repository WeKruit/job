from __future__ import annotations

from app.contracts.sync import SourceSyncResult, SourceSyncStats
from app.ingest.fetchers.base import BaseFetcher
from app.ingest.mappers.base import BaseMapper
from app.models import Source, build_source_key
from app.services.application.blob.job_blob import JobBlobManager

from .finalize import finalize_snapshot
from .mapping import dedupe_by_external_job_id, map_raw_jobs
from .staging import (
    DEFAULT_BLOB_SYNC_CONCURRENCY,
    build_existing_map,
    persist_staged_jobs,
    stage_jobs_for_snapshot,
)
from .location_sync import sync_staged_job_locations
from .errors import FullSnapshotSyncError
from .time_utils import now_naive_utc


class FullSnapshotSyncService:
    """Same-source full snapshot reconcile service."""

    def __init__(
        self,
        session=None,
        job_repository=None,
        location_repo=None,
        job_location_repo=None,
        blob_manager: JobBlobManager | None = None,
        blob_sync_concurrency: int = DEFAULT_BLOB_SYNC_CONCURRENCY,
    ):
        self.session = session
        if job_repository is None and session is not None:
            from app.repositories.job import JobRepository

            job_repository = JobRepository(session)
        if job_repository is None:
            raise ValueError("Either session or job_repository must be provided")
        self.job_repository = job_repository
        self.location_repo = location_repo
        self.job_location_repo = job_location_repo
        self.blob_manager = blob_manager or JobBlobManager()
        self.blob_sync_concurrency = max(1, int(blob_sync_concurrency))

    async def sync_source(
        self,
        *,
        source: Source,
        fetcher: BaseFetcher,
        mapper: BaseMapper,
        include_content: bool = True,
        dry_run: bool = False,
    ) -> SourceSyncResult:
        source_key = build_source_key(source.platform, source.identifier)
        source_id = str(source.id)
        stats = SourceSyncStats()

        try:
            raw_jobs = await fetcher.fetch(source.identifier, include_content=include_content)
            stats.fetched_count = len(raw_jobs)
            mapped_payloads = map_raw_jobs(
                raw_jobs=raw_jobs,
                mapper=mapper,
                source_id=source_id,
                source_key=source_key,
            )
            stats.mapped_count = len(mapped_payloads)
            unique_payloads = dedupe_by_external_job_id(mapped_payloads)
            stats.unique_count = len(unique_payloads)
            stats.deduped_by_external_id = stats.mapped_count - stats.unique_count

            sync_started_at = now_naive_utc()
            existing_map = await build_existing_map(
                job_repository=self.job_repository,
                source_id=source_id,
                unique_payloads=unique_payloads,
            )
            staged_jobs = await stage_jobs_for_snapshot(
                blob_manager=self.blob_manager,
                unique_payloads=unique_payloads,
                existing_map=existing_map,
                sync_started_at=sync_started_at,
                stats=stats,
                blob_sync_concurrency=self.blob_sync_concurrency,
            )
            if staged_jobs:
                await persist_staged_jobs(job_repository=self.job_repository, staged_jobs=staged_jobs)
                await sync_staged_job_locations(
                    session=self.session,
                    location_repo=self.location_repo,
                    job_location_repo=self.job_location_repo,
                    staged_jobs=staged_jobs,
                    unique_payloads=unique_payloads,
                )
                # Re-save jobs to persist updated compatibility fields if primary was synced
                await persist_staged_jobs(job_repository=self.job_repository, staged_jobs=staged_jobs)

            await finalize_snapshot(
                session=self.session,
                job_repository=self.job_repository,
                source_id=source_id,
                sync_started_at=sync_started_at,
                dry_run=dry_run,
                stats=stats,
            )

            return SourceSyncResult(
                source_id=source_id,
                source_key=source_key,
                ok=True,
                stats=stats,
            )
        except Exception as exc:
            if self.session is not None:
                await self.session.rollback()
            stats.failed_count = max(
                stats.unique_count,
                stats.mapped_count,
                stats.fetched_count,
                1,
            )
            return SourceSyncResult(
                source_id=source_id,
                source_key=source_key,
                ok=False,
                stats=stats,
                error=str(exc),
            )


__all__ = ["FullSnapshotSyncService", "FullSnapshotSyncError"]
