from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.match import MatchRequest, MatchResponse
from app.services.application.match_service import (
    LLMRerankConfigurationError,
    MatchExperimentService,
    MatchQueryError,
)

router = APIRouter(prefix="/matching", tags=["matching"])


@lru_cache
def get_match_service() -> MatchExperimentService:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.firestore_credentials_file:
        from app.infrastructure.firestore_client import get_firestore_client
        from app.services.infra.matching.firestore_query import FirestoreMatchCandidateGateway

        db = get_firestore_client()
        return MatchExperimentService(
            candidate_gateway=FirestoreMatchCandidateGateway(db),
        )
    return MatchExperimentService()


@router.post(
    "/recommendations",
    response_model=MatchResponse,
    responses={
        503: {"description": "Matching dependencies unavailable"},
    },
)
async def get_match_recommendations(
    request: MatchRequest,
    service: MatchExperimentService = Depends(get_match_service),
) -> MatchResponse:
    try:
        return await service.run(request)
    except (MatchQueryError, LLMRerankConfigurationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
