"""Job API endpoints with source_id compatibility support."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.core.deps import get_job_service
from app.models import Job, JobStatus, build_source_key
from app.schemas.location import JobLocationRead
from app.schemas.job import JobCreate, JobRead, JobUpdate
from app.services.application.job_service import (
    JobNotFoundError,
    JobService,
    SourceIdNotFoundError,
    SourceResolutionError,
)
from app.services.infra.blob_storage import BlobNotFoundError, BlobStorageNotConfiguredError

router = APIRouter(prefix="/jobs", tags=["jobs"])


async def _map_job_to_read(
    job: Job,
    *,
    service: JobService,
    include_blob_content: bool,
) -> JobRead:
    """Map a Job model to JobRead, injecting locations and optional blob content."""
    data = job.model_dump()
    data["description_html"] = None
    data["raw_payload"] = {}

    # Try SQLAlchemy instance_state first (SQL path), fall back for Firestore path
    try:
        from sqlalchemy.orm.attributes import instance_state

        state = instance_state(job)
        source_record = state.dict.get("source_record")
        if source_record is not None:
            data["source"] = build_source_key(source_record.platform, source_record.identifier)
        else:
            data["source"] = None
        if "job_locations" in state.dict:
            links = state.dict["job_locations"]
            data["locations"] = [
                JobLocationRead.model_validate(link).model_dump() for link in links
            ]
        else:
            data["locations"] = []
    except Exception:
        # Firestore path: no SQLAlchemy state
        data["source"] = None
        data["locations"] = []

    if include_blob_content:
        try:
            data["description_html"] = await service.blob_manager.load_description_html(job)
        except (BlobNotFoundError, BlobStorageNotConfiguredError, Exception):
            data["description_html"] = None
        try:
            raw_payload = await service.blob_manager.load_raw_payload(job)
            data["raw_payload"] = raw_payload if isinstance(raw_payload, dict) else {}
        except (BlobNotFoundError, BlobStorageNotConfiguredError, Exception):
            data["raw_payload"] = {}

    return JobRead.model_validate(data)


@router.get("/", response_model=list[JobRead])
async def list_jobs(
    skip: int = 0,
    limit: int = 100,
    status: JobStatus | None = None,
    include_blob_content: bool = False,
    service: JobService = Depends(get_job_service),
) -> list[JobRead]:
    jobs = await service.list_jobs(skip=skip, limit=limit, status=status)
    return list(
        await asyncio.gather(
            *(
                _map_job_to_read(
                    job,
                    service=service,
                    include_blob_content=include_blob_content,
                )
                for job in jobs
            )
        )
    )


@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
) -> JobRead:
    try:
        job = await service.get_job(job_id)
        return await _map_job_to_read(job, service=service, include_blob_content=True)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.post("/", response_model=JobRead, status_code=201)
async def create_job(
    job_in: JobCreate,
    service: JobService = Depends(get_job_service),
) -> JobRead:
    try:
        job = await service.create_job(job_in)
        return await _map_job_to_read(job, service=service, include_blob_content=True)
    except (SourceResolutionError, SourceIdNotFoundError) as e:
        raise HTTPException(status_code=422, detail=e.message)


@router.patch("/{job_id}", response_model=JobRead)
async def update_job(
    job_id: str,
    job_in: JobUpdate,
    service: JobService = Depends(get_job_service),
) -> JobRead:
    try:
        job = await service.update_job(job_id, job_in)
        return await _map_job_to_read(job, service=service, include_blob_content=True)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
) -> None:
    try:
        await service.delete_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
