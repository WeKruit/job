"""Firestore-backed SyncRunRepository."""

from __future__ import annotations

from google.cloud.firestore_v1.async_client import AsyncClient

from app.contracts.sync import SourceSyncStats
from app.models import SyncRun, SyncRunStatus
from app.repositories.firestore._base import (
    FirestoreBaseRepository,
    doc_to_dict,
    new_id,
    utc_now,
)


def _apply_stats(data: dict, stats: SourceSyncStats | None) -> None:
    if stats is None:
        return
    data["fetched_count"] = stats.fetched_count
    data["mapped_count"] = stats.mapped_count
    data["unique_count"] = stats.unique_count
    data["deduped_by_external_id"] = stats.deduped_by_external_id
    data["deduped_by_apply_url"] = stats.deduped_by_apply_url
    data["inserted_count"] = stats.inserted_count
    data["updated_count"] = stats.updated_count
    data["closed_count"] = stats.closed_count
    data["failed_count"] = stats.failed_count


def _run_to_doc(run: SyncRun) -> dict:
    return {
        "source_id": run.source_id,
        "status": run.status.value if isinstance(run.status, SyncRunStatus) else run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "fetched_count": run.fetched_count,
        "mapped_count": run.mapped_count,
        "unique_count": run.unique_count,
        "deduped_by_external_id": run.deduped_by_external_id,
        "deduped_by_apply_url": run.deduped_by_apply_url,
        "inserted_count": run.inserted_count,
        "updated_count": run.updated_count,
        "closed_count": run.closed_count,
        "failed_count": run.failed_count,
        "error_summary": run.error_summary,
        "created_at": run.created_at,
    }


def _doc_to_run(data: dict) -> SyncRun:
    return SyncRun(
        id=data["id"],
        source_id=data.get("source_id"),
        status=SyncRunStatus(data["status"]),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        fetched_count=data.get("fetched_count", 0),
        mapped_count=data.get("mapped_count", 0),
        unique_count=data.get("unique_count", 0),
        deduped_by_external_id=data.get("deduped_by_external_id", 0),
        deduped_by_apply_url=data.get("deduped_by_apply_url", 0),
        inserted_count=data.get("inserted_count", 0),
        updated_count=data.get("updated_count", 0),
        closed_count=data.get("closed_count", 0),
        failed_count=data.get("failed_count", 0),
        error_summary=data.get("error_summary"),
        created_at=data.get("created_at"),
    )


class FirestoreSyncRunRepository(FirestoreBaseRepository):
    def __init__(self, db: AsyncClient):
        super().__init__(db, "sync_runs")

    async def create_running(self, *, source_id: str | None = None) -> SyncRun:
        run = await self.try_create_running(source_id=source_id)
        if run is None:
            raise RuntimeError("running sync already exists for source")
        return run

    async def try_create_running(self, *, source_id: str | None = None) -> SyncRun | None:
        # Check for existing running sync for this source
        if source_id:
            # Use a lock document to prevent concurrent syncs for the same source.
            # create() fails if the document already exists, providing atomicity.
            lock_ref = self._db.collection("sync_locks").document(f"source_{source_id}")
            try:
                await lock_ref.create({"source_id": source_id, "locked_at": utc_now()})
            except Exception:
                # Lock document already exists — another sync is running
                # Verify it's actually still running (not a stale lock)
                existing = await self.get_running_by_source_id(source_id=source_id)
                if existing is not None:
                    return None
                # Stale lock — delete and retry once
                await lock_ref.delete()
                try:
                    await lock_ref.create({"source_id": source_id, "locked_at": utc_now()})
                except Exception:
                    return None

        now = utc_now()
        run = SyncRun(
            id=new_id(),
            source_id=source_id,
            status=SyncRunStatus.running,
            started_at=now,
            created_at=now,
        )
        await self.collection.document(run.id).set(_run_to_doc(run))
        return run

    async def create_finished(
        self,
        *,
        source_id: str | None = None,
        status: SyncRunStatus,
        error_summary: str | None = None,
        stats: SourceSyncStats | None = None,
    ) -> SyncRun:
        now = utc_now()
        run = SyncRun(
            id=new_id(),
            source_id=source_id,
            status=status,
            started_at=now,
            finished_at=now,
            error_summary=error_summary,
            created_at=now,
        )
        doc_data = _run_to_doc(run)
        _apply_stats(doc_data, stats)
        await self.collection.document(run.id).set(doc_data)
        # Sync stats back to the model
        if stats:
            _apply_stats_to_model(run, stats)
        return run

    async def get_running_by_source_id(self, *, source_id: str) -> SyncRun | None:
        query = (
            self.collection
            .where("source_id", "==", source_id)
            .where("status", "==", SyncRunStatus.running.value)
            .order_by("started_at", direction="DESCENDING")
            .limit(1)
        )
        docs = []
        async for doc in query.stream():
            docs.append(doc)
        if not docs:
            return None
        data = doc_to_dict(docs[0])
        return _doc_to_run(data)

    async def has_any_for_source_id(self, *, source_id: str) -> bool:
        query = self.collection.where("source_id", "==", source_id).limit(1)
        async for _ in query.stream():
            return True
        return False

    async def finish(
        self,
        *,
        run: SyncRun,
        status: SyncRunStatus,
        error_summary: str | None = None,
        stats: SourceSyncStats | None = None,
    ) -> SyncRun:
        run.status = status
        run.finished_at = utc_now()
        run.error_summary = error_summary
        if stats:
            _apply_stats_to_model(run, stats)
        await self.collection.document(run.id).set(_run_to_doc(run))
        # Release the sync lock if one was acquired
        if run.source_id:
            lock_ref = self._db.collection("sync_locks").document(f"source_{run.source_id}")
            await lock_ref.delete()
        return run


def _apply_stats_to_model(run: SyncRun, stats: SourceSyncStats) -> None:
    run.fetched_count = stats.fetched_count
    run.mapped_count = stats.mapped_count
    run.unique_count = stats.unique_count
    run.deduped_by_external_id = stats.deduped_by_external_id
    run.deduped_by_apply_url = stats.deduped_by_apply_url
    run.inserted_count = stats.inserted_count
    run.updated_count = stats.updated_count
    run.closed_count = stats.closed_count
    run.failed_count = stats.failed_count
