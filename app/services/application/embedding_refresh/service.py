"""Snapshot-aligned embedding refresh service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import get_settings
from app.repositories.job import JobRepository
from app.repositories.job_embedding import JobEmbeddingRepository, JobEmbeddingUpsertPayload
from app.services.infra.embedding import (
    EmbeddingConfig,
    embed_texts,
    get_embedding_config,
    resolve_active_job_embedding_target,
)


@dataclass(frozen=True)
class EmbeddingRefreshExecutionResult:
    """Execution summary for one embedding refresh run."""

    source_id: str
    snapshot_run_id: str | None
    triggered: bool
    selected_jobs: int = 0
    attempted_jobs: int = 0
    refreshed_jobs: int = 0
    failed_jobs: int = 0
    skipped_jobs: int = 0
    error: str | None = None


class EmbeddingRefreshServiceInterface(Protocol):
    """Interface used by sync orchestration for refresh execution."""

    async def refresh_for_source(
        self,
        *,
        source_id: str,
        snapshot_run_id: str | None = None,
    ) -> EmbeddingRefreshExecutionResult: ...


class EmbeddingRefreshService:
    """Refresh active-target embeddings for one source after successful snapshots."""

    def __init__(
        self,
        *,
        session: AsyncSession | None = None,
        job_repository: JobRepository | None = None,
        job_embedding_repository: JobEmbeddingRepository | None = None,
        embedding_fn=embed_texts,
        settings_provider=get_settings,
        embedding_config_provider=get_embedding_config,
        target_resolver=resolve_active_job_embedding_target,
    ) -> None:
        self.session = session
        if job_repository is None:
            if session is None:
                raise ValueError("session or job_repository required")
            job_repository = JobRepository(session)
        if job_embedding_repository is None:
            if session is None:
                raise ValueError("session or job_embedding_repository required")
            job_embedding_repository = JobEmbeddingRepository(session)
        self.job_repository = job_repository
        self.job_embedding_repository = job_embedding_repository
        self.embedding_fn = embedding_fn
        self.settings_provider = settings_provider
        self.embedding_config_provider = embedding_config_provider
        self.target_resolver = target_resolver

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def refresh_for_source(
        self,
        *,
        source_id: str,
        snapshot_run_id: str | None = None,
    ) -> EmbeddingRefreshExecutionResult:
        settings = self.settings_provider()
        if not getattr(settings, "embedding_refresh_enabled", True):
            return EmbeddingRefreshExecutionResult(
                source_id=source_id,
                snapshot_run_id=snapshot_run_id,
                triggered=False,
            )

        batch_size = max(1, int(getattr(settings, "embedding_refresh_batch_size", 1)))
        embedding_dim = int(getattr(settings, "embedding_dim"))
        config: EmbeddingConfig = self.embedding_config_provider()
        target = self.target_resolver(
            config=config,
            embedding_dim=embedding_dim,
        )

        selected_jobs = 0
        attempted_jobs = 0
        refreshed_jobs = 0
        failed_jobs = 0
        last_id: str | None = None

        while True:
            candidates = await self.job_repository.list_snapshot_refresh_candidates_for_active_target(
                source_id=source_id,
                embedding_kind=target.embedding_kind,
                embedding_target_revision=target.embedding_target_revision,
                embedding_model=target.embedding_model,
                embedding_dim=target.embedding_dim,
                last_id=last_id,
                limit=batch_size,
            )
            if not candidates:
                break

            selected_jobs += len(candidates)
            attempted_jobs += len(candidates)
            last_id = candidates[-1].id

            try:
                vectors = await self.embedding_fn(
                    [candidate.description for candidate in candidates],
                    config=config,
                    dimensions=target.embedding_dim,
                )
                payloads = [
                    JobEmbeddingUpsertPayload(
                        job_id=candidate.id,
                        embedding=vectors[index],
                        content_fingerprint=candidate.content_fingerprint,
                    )
                    for index, candidate in enumerate(candidates)
                ]
                upserted = await self.job_embedding_repository.upsert_many_for_target(
                    rows=payloads,
                    embedding_kind=target.embedding_kind,
                    embedding_target_revision=target.embedding_target_revision,
                    embedding_model=target.embedding_model,
                    embedding_dim=target.embedding_dim,
                    updated_at=self._now(),
                )
                refreshed_jobs += upserted
                failed_jobs += max(0, len(candidates) - upserted)
            except Exception as exc:  # noqa: BLE001
                failed_jobs += len(candidates)
                if self.session is not None:
                    await self.session.rollback()
                return EmbeddingRefreshExecutionResult(
                    source_id=source_id,
                    snapshot_run_id=snapshot_run_id,
                    triggered=True,
                    selected_jobs=selected_jobs,
                    attempted_jobs=attempted_jobs,
                    refreshed_jobs=refreshed_jobs,
                    failed_jobs=failed_jobs,
                    error=str(exc),
                )

        if self.session is not None:
            await self.session.commit()
        return EmbeddingRefreshExecutionResult(
            source_id=source_id,
            snapshot_run_id=snapshot_run_id,
            triggered=True,
            selected_jobs=selected_jobs,
            attempted_jobs=attempted_jobs,
            refreshed_jobs=refreshed_jobs,
            failed_jobs=failed_jobs,
        )
