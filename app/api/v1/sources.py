"""
Source API endpoints.

Provides REST API endpoints for Source management.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.deps import get_source_service
from app.models import PlatformType
from app.schemas.source import (
    SourceCreate,
    SourceRead,
    SourceResponse,
    SourceListResponse,
    SourceSlugListResponse,
    ErrorResponse,
    ErrorDetail,
    SourceUpdate,
    DeleteResponse,
)
from app.services.application.source_service import (
    SourceService,
    DuplicateNameError,
    DuplicateIdentifierError,
    SourceNotFoundError,
    HasReferencesError,
    HasMutationBlockError,
)

router = APIRouter(prefix="/sources", tags=["sources"])


@router.post(
    "/",
    response_model=SourceResponse,
    status_code=201,
    responses={
        409: {"model": ErrorResponse, "description": "Duplicate name or identifier"},
        422: {"description": "Validation error"},
    },
)
async def create_source(
    source_in: SourceCreate,
    service: SourceService = Depends(get_source_service),
):
    """
    Create a new source.

    Returns 409 Conflict if a source with the same name (case-insensitive) exists.
    """
    try:
        source = await service.create_source(source_in)
        return SourceResponse(
            success=True, data=SourceRead.model_validate(source), message="操作成功"
        )
    except DuplicateNameError as e:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )
    except DuplicateIdentifierError as e:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )


@router.get(
    "/",
    response_model=SourceListResponse,
)
async def list_sources(
    enabled: bool | None = None,
    platform: PlatformType | None = None,
    service: SourceService = Depends(get_source_service),
) -> SourceListResponse:
    """
    List sources with optional enabled/platform filters.

    Args:
        enabled: Filter by enabled status. None returns all.
        platform: Filter by platform. None returns all platforms.
    """
    sources = await service.list_sources(enabled=enabled, platform=platform)
    return SourceListResponse(
        success=True, data=[SourceRead.model_validate(s) for s in sources], total=len(sources)
    )


@router.get(
    "/slugs",
    response_model=SourceSlugListResponse,
)
async def list_source_slugs(
    platform: PlatformType = PlatformType.GREENHOUSE,
    enabled: bool = True,
    service: SourceService = Depends(get_source_service),
) -> SourceSlugListResponse:
    """List source slugs (identifiers) for a platform."""
    slugs = await service.list_slugs(platform=platform, enabled=enabled)
    return SourceSlugListResponse(
        success=True,
        platform=platform,
        data=slugs,
        total=len(slugs),
    )


@router.get(
    "/{source_id}",
    response_model=SourceResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Source not found"},
    },
)
async def get_source(
    source_id: str,
    service: SourceService = Depends(get_source_service),
):
    """Get a source by ID."""
    try:
        source = await service.get_source(source_id)
        return SourceResponse(
            success=True, data=SourceRead.model_validate(source), message="操作成功"
        )
    except SourceNotFoundError as e:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )


@router.patch(
    "/{source_id}",
    response_model=SourceResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Source not found"},
        409: {"model": ErrorResponse, "description": "Duplicate name or identifier"},
    },
)
async def update_source(
    source_id: str,
    source_in: SourceUpdate,
    service: SourceService = Depends(get_source_service),
):
    """
    Update a source (partial update).

    Supports updating individual fields.
    """
    try:
        source = await service.update_source(source_id, source_in)
        return SourceResponse(
            success=True, data=SourceRead.model_validate(source), message="操作成功"
        )
    except SourceNotFoundError as e:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )
    except DuplicateNameError as e:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )
    except DuplicateIdentifierError as e:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )
    except HasMutationBlockError as e:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )


@router.delete(
    "/{source_id}",
    response_model=DeleteResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Source not found"},
        409: {"model": ErrorResponse, "description": "Has references"},
    },
)
async def delete_source(
    source_id: str,
    service: SourceService = Depends(get_source_service),
):
    """
    Delete a source.

    Only allowed if the source has no associated SyncRun records.
    """
    try:
        await service.delete_source(source_id)
        return DeleteResponse(success=True, message="数据源已删除")
    except SourceNotFoundError as e:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )
    except HasReferencesError as e:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                success=False, error=ErrorDetail(code=e.code, message=e.message)
            ).model_dump(),
        )
