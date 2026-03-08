from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from app.contracts.sync import SourceSyncResult
from app.core.config import get_settings
from app.ingest.fetchers.base import BaseFetcher
from app.ingest.mappers.base import BaseMapper
from app.models import PlatformType, Source, SyncRun, SyncRunStatus
from app.services.application.embedding_refresh import (
    EmbeddingRefreshService,
    EmbeddingRefreshServiceInterface,
)
from app.services.application.full_snapshot_sync import FullSnapshotSyncService

from .handlers import PLATFORM_SYNC_HANDLERS, PlatformSyncHandlers


class SourceSyncAttemptFailed(Exception):
    """Raised to trigger retry when a source sync result is unsuccessful."""

    def __init__(self, result: SourceSyncResult):
        self.result = result
        super().__init__(result.error or "source sync failed")


class SyncService:
    """Orchestrates one source sync run with overlap guard and retries."""

    def __init__(
        self,
        engine: AsyncEngine | None = None,
        embedding_refresh_factory: (
            Callable[[AsyncSession], EmbeddingRefreshServiceInterface] | None
        ) = None,
    ):
        self.engine = engine
        self.embedding_refresh_factory = embedding_refresh_factory or (
            lambda session: EmbeddingRefreshService(session=session)
        )
        settings = get_settings()
        self._use_firestore = bool(settings.firestore_credentials_file)

    def _build_embedding_refresh_service(
        self,
        session: AsyncSession,
    ) -> EmbeddingRefreshServiceInterface:
        return self.embedding_refresh_factory(session)

    async def _refresh_embeddings_if_needed(
        self,
        *,
        source_id: str,
        sync_run: SyncRun,
        dry_run: bool,
    ) -> None:
        if dry_run or sync_run.status != SyncRunStatus.success:
            return
        if self._use_firestore:
            from app.infrastructure.firestore_client import get_firestore_client
            from app.repositories.firestore import (
                FirestoreJobEmbeddingRepository,
                FirestoreJobRepository,
            )

            db = get_firestore_client()
            refresh_service = EmbeddingRefreshService(
                session=None,
                job_repository=FirestoreJobRepository(db),
                job_embedding_repository=FirestoreJobEmbeddingRepository(db),
            )
            try:
                await refresh_service.refresh_for_source(
                    source_id=source_id,
                    snapshot_run_id=str(sync_run.id),
                )
            except Exception:  # noqa: BLE001
                pass  # embedding failure should not fail the sync
            return
        async with AsyncSession(self.engine) as refresh_session:
            refresh_service = self._build_embedding_refresh_service(refresh_session)
            try:
                await refresh_service.refresh_for_source(
                    source_id=source_id,
                    snapshot_run_id=str(sync_run.id),
                )
            except Exception:  # noqa: BLE001
                await refresh_session.rollback()

    async def sync_source(
        self,
        *,
        source: Source,
        include_content: bool = True,
        dry_run: bool = False,
        retry_attempts: int = 3,
    ) -> SyncRun:
        if self._use_firestore:
            return await self._sync_source_firestore(
                source=source,
                include_content=include_content,
                dry_run=dry_run,
                retry_attempts=retry_attempts,
            )
        return await self._sync_source_sql(
            source=source,
            include_content=include_content,
            dry_run=dry_run,
            retry_attempts=retry_attempts,
        )

    # ------------------------------------------------------------------ #
    # Firestore path                                                       #
    # ------------------------------------------------------------------ #

    async def _sync_source_firestore(
        self,
        *,
        source: Source,
        include_content: bool,
        dry_run: bool,
        retry_attempts: int,
    ) -> SyncRun:
        from app.infrastructure.firestore_client import get_firestore_client
        from app.repositories.firestore import (
            FirestoreJobLocationRepository,
            FirestoreJobRepository,
            FirestoreLocationRepository,
            FirestoreSyncRunRepository,
        )

        db = get_firestore_client()
        sync_run_repo = FirestoreSyncRunRepository(db)

        handlers = self._resolve_handlers(source.platform)

        # Overlap guard
        running = await sync_run_repo.get_running_by_source_id(source_id=str(source.id))
        if running is not None:
            return await sync_run_repo.create_finished(
                source_id=str(source.id),
                status=SyncRunStatus.failed,
                error_summary="overlap: source already running",
            )

        if handlers is None:
            return await sync_run_repo.create_finished(
                source_id=str(source.id),
                status=SyncRunStatus.failed,
                error_summary=f"unsupported platform: {source.platform.value}",
            )

        sync_run = await sync_run_repo.try_create_running(source_id=str(source.id))
        if sync_run is None:
            return await sync_run_repo.create_finished(
                source_id=str(source.id),
                status=SyncRunStatus.failed,
                error_summary="overlap: source already running",
            )

        fetcher = handlers.fetcher_cls()
        mapper = handlers.mapper_cls()
        last_result: SourceSyncResult | None = None

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max(1, retry_attempts)),
                wait=wait_exponential(multiplier=1, min=1, max=4),
                reraise=True,
            ):
                with attempt:
                    job_repo = FirestoreJobRepository(db)
                    location_repo = FirestoreLocationRepository(db)
                    job_location_repo = FirestoreJobLocationRepository(db)

                    from app.services.application.blob.job_blob import JobBlobManager
                    from app.services.infra.blob_storage import NoOpBlobStorage

                    service = FullSnapshotSyncService(
                        session=None,
                        job_repository=job_repo,
                        location_repo=location_repo,
                        job_location_repo=job_location_repo,
                        blob_manager=JobBlobManager(storage=NoOpBlobStorage()),
                    )
                    result = await service.sync_source(
                        source=source,
                        fetcher=fetcher,
                        mapper=mapper,
                        include_content=include_content,
                        dry_run=dry_run,
                    )
                    if not result.ok:
                        last_result = result
                        raise SourceSyncAttemptFailed(result)
                    last_result = result

            if last_result is None:
                return await sync_run_repo.finish(
                    run=sync_run,
                    status=SyncRunStatus.failed,
                    error_summary="source sync produced no result",
                )

            finished_run = await sync_run_repo.finish(
                run=sync_run,
                status=SyncRunStatus.success,
                stats=last_result.stats,
            )
            await self._refresh_embeddings_if_needed(
                source_id=str(source.id),
                sync_run=finished_run,
                dry_run=dry_run,
            )
            return finished_run
        except SourceSyncAttemptFailed as exc:
            failed_result = last_result or exc.result
            return await sync_run_repo.finish(
                run=sync_run,
                status=SyncRunStatus.failed,
                error_summary=failed_result.error or "source sync failed",
                stats=failed_result.stats,
            )
        except Exception as exc:
            return await sync_run_repo.finish(
                run=sync_run,
                status=SyncRunStatus.failed,
                error_summary=str(exc),
            )

    # ------------------------------------------------------------------ #
    # SQL path (original)                                                  #
    # ------------------------------------------------------------------ #

    async def _sync_source_sql(
        self,
        *,
        source: Source,
        include_content: bool,
        dry_run: bool,
        retry_attempts: int,
    ) -> SyncRun:
        from app.repositories.job import JobRepository
        from app.repositories.sync_run import SyncRunRepository

        handlers = self._resolve_handlers(source.platform)

        async with AsyncSession(self.engine) as tracking_session:
            sync_run_repository = SyncRunRepository(tracking_session)
            running = await sync_run_repository.get_running_by_source_id(source_id=str(source.id))
            if running is not None:
                return await sync_run_repository.create_finished(
                    source_id=str(source.id),
                    status=SyncRunStatus.failed,
                    error_summary="overlap: source already running",
                )

            if handlers is None:
                return await sync_run_repository.create_finished(
                    source_id=str(source.id),
                    status=SyncRunStatus.failed,
                    error_summary=f"unsupported platform: {source.platform.value}",
                )

            sync_run = await sync_run_repository.try_create_running(
                source_id=str(source.id),
            )
            if sync_run is None:
                return await sync_run_repository.create_finished(
                    source_id=str(source.id),
                    status=SyncRunStatus.failed,
                    error_summary="overlap: source already running",
                )
            fetcher = handlers.fetcher_cls()
            mapper = handlers.mapper_cls()
            last_result: SourceSyncResult | None = None

            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(max(1, retry_attempts)),
                    wait=wait_exponential(multiplier=1, min=1, max=4),
                    reraise=True,
                ):
                    with attempt:
                        result = await self._execute_snapshot_sync_sql(
                            source=source,
                            fetcher=fetcher,
                            mapper=mapper,
                            include_content=include_content,
                            dry_run=dry_run,
                        )
                        if not result.ok:
                            last_result = result
                            raise SourceSyncAttemptFailed(result)
                        last_result = result

                if last_result is None:
                    return await sync_run_repository.finish(
                        run=sync_run,
                        status=SyncRunStatus.failed,
                        error_summary="source sync produced no result",
                    )

                finished_run = await sync_run_repository.finish(
                    run=sync_run,
                    status=SyncRunStatus.success,
                    stats=last_result.stats,
                )
                await self._refresh_embeddings_if_needed(
                    source_id=str(source.id),
                    sync_run=finished_run,
                    dry_run=dry_run,
                )
                return finished_run
            except SourceSyncAttemptFailed as exc:
                failed_result = last_result or exc.result
                return await sync_run_repository.finish(
                    run=sync_run,
                    status=SyncRunStatus.failed,
                    error_summary=failed_result.error or "source sync failed",
                    stats=failed_result.stats,
                )
            except Exception as exc:
                return await sync_run_repository.finish(
                    run=sync_run,
                    status=SyncRunStatus.failed,
                    error_summary=str(exc),
                )

    @staticmethod
    def _resolve_handlers(platform: PlatformType) -> PlatformSyncHandlers | None:
        return PLATFORM_SYNC_HANDLERS.get(platform)

    async def _execute_snapshot_sync_sql(
        self,
        *,
        source: Source,
        fetcher: BaseFetcher,
        mapper: BaseMapper,
        include_content: bool,
        dry_run: bool,
    ) -> SourceSyncResult:
        from app.repositories.job import JobRepository

        async with AsyncSession(self.engine) as session:
            service = FullSnapshotSyncService(
                session=session,
                job_repository=JobRepository(session),
            )
            return await service.sync_source(
                source=source,
                fetcher=fetcher,
                mapper=mapper,
                include_content=include_content,
                dry_run=dry_run,
            )
