from __future__ import annotations

from app.core.config import get_settings
from app.schemas.match import (
    HardFilterSummary,
    LLMRerankSummary,
    MatchRequest,
    MatchResponse,
    MatchResponseMeta,
    MatchResultItem,
    SQLPrefilterSummary,
    VectorThresholdSummary,
)
from app.services.domain.matching import (
    build_user_embedding_text,
    build_user_skill_tokens,
    filter_match_candidates_by_min_cosine_score,
    hard_filter_match_candidates,
    infer_needs_sponsorship,
    infer_user_degree_rank,
    infer_user_job_domain,
    infer_user_seniority_level,
    rerank_match_candidates,
    to_int,
)
from app.services.infra.embedding import (
    embed_text,
    get_embedding_config,
    resolve_active_job_embedding_target,
)
from app.services.infra.matching.llm_rerank import LLMMatchReranker
from app.services.infra.matching.query import MatchCandidateGateway
from app.services.infra.matching.query import (
    build_sql_prefilter,
    vector_literal,
)
from app.services.infra.matching.firestore_query import FirestoreMatchCandidateGateway


class MatchServiceError(Exception):
    """Base exception for matching service failures."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class MatchQueryError(MatchServiceError):
    """Raised when the candidate recall query fails."""

    def __init__(self, message: str = "Failed to fetch match candidates"):
        super().__init__("MATCH_QUERY_ERROR", message)


class LLMRerankConfigurationError(MatchServiceError):
    """Raised when LLM rerank is requested without valid LLM configuration."""

    def __init__(self, message: str = "LLM rerank requested but LLM is not configured"):
        super().__init__("LLM_RERANK_CONFIG_ERROR", message)


class MatchExperimentService:
    """Application service for the offline match experiment pipeline."""

    def __init__(
        self,
        *,
        candidate_gateway: MatchCandidateGateway | None = None,
        llm_reranker: LLMMatchReranker | None = None,
        embedding_fn=None,
        embedding_config_provider=None,
        settings_provider=None,
        active_target_resolver=None,
    ):
        self.settings_provider = settings_provider or get_settings
        self.embedding_fn = embedding_fn or embed_text
        self.embedding_config_provider = embedding_config_provider or get_embedding_config
        self.active_target_resolver = active_target_resolver or resolve_active_job_embedding_target
        self.candidate_gateway = candidate_gateway or MatchCandidateGateway(
            settings_provider=self.settings_provider
        )
        self.llm_reranker = llm_reranker or LLMMatchReranker()

    async def run(self, request: MatchRequest) -> MatchResponse:
        legacy_user_data = request.candidate.to_matching_payload()
        user_years = request.candidate.total_years_experience
        user_years_for_rerank = to_int(request.candidate.total_years_experience, default=0)
        user_degree_rank = infer_user_degree_rank(legacy_user_data)
        needs_sponsorship = infer_needs_sponsorship(
            legacy_user_data,
            request.needs_sponsorship_override,
        )
        user_skill_tokens = build_user_skill_tokens(legacy_user_data)
        user_domain = infer_user_job_domain(legacy_user_data)
        user_seniority = infer_user_seniority_level(legacy_user_data)

        user_text = build_user_embedding_text(
            legacy_user_data,
            max_chars=request.max_user_chars,
        )
        settings = self.settings_provider()
        user_embedding = await self.embedding_fn(
            user_text,
            config=self.embedding_config_provider(),
            dimensions=settings.embedding_dim,
        )
        try:
            if isinstance(self.candidate_gateway, FirestoreMatchCandidateGateway):
                candidate_rows = await self.candidate_gateway.fetch_candidates(
                    user_embedding=user_embedding,
                    top_k=request.top_k,
                    exclude_job_ids=request.exclude_job_ids or None,
                )
                sql_prefilter_summary = {
                    "sponsorship_filter_applied": False,
                    "degree_filter_applied": False,
                    "preferred_country_code": request.preferred_country_code,
                    "user_degree_rank": user_degree_rank,
                }
            else:
                user_vec_literal = vector_literal(user_embedding)
                sql_prefilter, sql_prefilter_params, sql_prefilter_summary = build_sql_prefilter(
                    start_index=6,
                    needs_sponsorship=needs_sponsorship,
                    user_degree_rank=user_degree_rank,
                    preferred_country_code=request.preferred_country_code,
                )
                active_target = self.active_target_resolver(
                    config=self.embedding_config_provider(),
                    embedding_dim=settings.embedding_dim,
                )
                candidate_rows = await self.candidate_gateway.fetch_candidates(
                    user_vec_literal=user_vec_literal,
                    top_k=request.top_k,
                    prefilter_sql=sql_prefilter,
                    prefilter_params=sql_prefilter_params,
                    embedding_kind=active_target.embedding_kind,
                    embedding_target_revision=active_target.embedding_target_revision,
                    embedding_model=active_target.embedding_model,
                    embedding_dim=active_target.embedding_dim,
                )
        except MatchServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MatchQueryError() from exc

        vector_filtered_rows, vector_threshold_summary = (
            filter_match_candidates_by_min_cosine_score(
                candidate_rows,
                min_cosine_score=request.min_cosine_score,
            )
        )
        hard_filtered_rows, hard_filter_summary = hard_filter_match_candidates(
            vector_filtered_rows,
            needs_sponsorship=False,
            user_years=user_years,
            user_degree_rank=-1,
            max_experience_gap=request.experience_buffer_years,
        )
        context_by_job_id = {
            str(row["job_id"]): row for row in hard_filtered_rows if row.get("job_id") is not None
        }

        ranked = rerank_match_candidates(
            hard_filtered_rows,
            user_years=user_years_for_rerank,
            user_degree_rank=user_degree_rank,
            user_skill_tokens=user_skill_tokens,
            user_domain=user_domain,
            user_seniority=user_seniority,
        )

        try:
            ranked, llm_rerank_summary = await self.llm_reranker.rerank_if_enabled(
                ranked,
                enabled=request.enable_llm_rerank,
                user_data=legacy_user_data,
                context_by_job_id=context_by_job_id,
                llm_top_n=request.llm_top_n,
                concurrency=request.llm_concurrency,
            )
        except ValueError as exc:
            raise LLMRerankConfigurationError(str(exc)) from exc

        top_results = ranked[: request.top_n]
        allowed_result_fields = set(MatchResultItem.model_fields.keys())
        normalized_results: list[dict[str, object]] = []
        for item in top_results:
            normalized = {
                key: value for key, value in dict(item).items() if key in allowed_result_fields
            }
            if not isinstance(normalized.get("locations"), list):
                normalized["locations"] = []
            normalized_results.append(normalized)

        return MatchResponse(
            meta=MatchResponseMeta(
                user_json=request.user_json,
                needs_sponsorship=needs_sponsorship,
                user_total_years_experience=user_years,
                user_degree_rank=user_degree_rank,
                user_skill_count=len(user_skill_tokens),
                user_domain=user_domain,
                user_seniority=user_seniority,
                top_k=request.top_k,
                top_n=request.top_n,
                sql_prefilter=SQLPrefilterSummary.model_validate(sql_prefilter_summary),
                candidates_after_sql_prefilter=len(candidate_rows),
                vector_threshold_summary=VectorThresholdSummary.model_validate(
                    vector_threshold_summary
                ),
                candidates_after_vector_threshold=len(vector_filtered_rows),
                candidates_before_hard_filter=len(vector_filtered_rows),
                candidates_after_hard_filter=len(hard_filtered_rows),
                hard_filter_summary=HardFilterSummary.model_validate(hard_filter_summary),
                llm_rerank_summary=LLMRerankSummary.model_validate(llm_rerank_summary),
                results_returned=len(normalized_results),
            ),
            results=[MatchResultItem.model_validate(item) for item in normalized_results],
        )
