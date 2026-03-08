from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.schemas.location import MatchLocationRead


class CandidateEducation(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    degree: str | None = None
    school: str | None = None
    field_of_study: str | None = Field(
        default=None,
        validation_alias=AliasChoices("fieldOfStudy", "field_of_study"),
        serialization_alias="fieldOfStudy",
    )


class CandidateWorkHistory(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    title: str | None = None
    company: str | None = None
    bullets: list[str] = Field(default_factory=list)
    description: str | None = None
    achievements: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    summary: str | None = None
    skills: list[str] = Field(default_factory=list)
    work_authorization: str | None = Field(
        default=None,
        validation_alias=AliasChoices("workAuthorization", "work_authorization"),
        serialization_alias="workAuthorization",
    )
    total_years_experience: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("totalYearsExperience", "total_years_experience"),
        serialization_alias="totalYearsExperience",
    )
    education: list[CandidateEducation] = Field(default_factory=list)
    work_history: list[CandidateWorkHistory] = Field(
        default_factory=list,
        validation_alias=AliasChoices("workHistory", "work_history"),
        serialization_alias="workHistory",
    )

    def to_matching_payload(self) -> dict[str, object]:
        return self.model_dump(by_alias=True, exclude_none=True)


class MatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    candidate: CandidateProfile
    top_k: int = Field(default=200, ge=1)
    top_n: int = Field(default=50, ge=1)
    needs_sponsorship_override: Literal["auto", "true", "false"] = "auto"
    experience_buffer_years: int = Field(default=1, ge=0)
    min_cosine_score: float = Field(default=0.48, ge=0.0, le=1.0)
    enable_llm_rerank: bool = False
    llm_top_n: int = Field(default=10, ge=1)
    llm_concurrency: int = Field(default=3, ge=1)
    max_user_chars: int = Field(default=12000, ge=1)
    preferred_country_code: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        pattern=r"^[A-Z]{2}$",
        validation_alias=AliasChoices("preferredCountryCode", "preferred_country_code"),
        serialization_alias="preferredCountryCode",
    )
    exclude_job_ids: list[str] = Field(
        default_factory=list,
        description="Job IDs to exclude from results (e.g. already applied/saved/recommended).",
        validation_alias=AliasChoices("excludeJobIds", "exclude_job_ids"),
        serialization_alias="excludeJobIds",
    )
    user_json: str | None = None


class SQLPrefilterSummary(BaseModel):
    sponsorship_filter_applied: bool
    degree_filter_applied: bool
    preferred_country_code: str | None = None
    user_degree_rank: int


class VectorThresholdSummary(BaseModel):
    enabled: bool
    input_count: int
    passed_count: int
    rejected_count: int
    min_cosine_score: float


class HardFilterRejectedByReason(BaseModel):
    sponsorship: int
    degree: int
    experience: int


class HardFilterConfig(BaseModel):
    needs_sponsorship: bool
    degree_filter_applied: bool
    experience_filter_applied: bool
    max_experience_gap: int


class HardFilterSummary(BaseModel):
    enabled: bool
    input_count: int
    passed_count: int
    rejected_count: int
    rejected_by_reason: HardFilterRejectedByReason
    config: HardFilterConfig


class TokenUsageSummary(BaseModel):
    total_requests: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    by_model: dict[str, dict[str, int]]


class LLMRerankSummary(BaseModel):
    enabled: bool
    window_size: int
    attempted_count: int
    succeeded_count: int
    failed_count: int
    concurrency: int
    reorder_applied: bool
    adjustment_map: dict[str, float]
    token_usage: TokenUsageSummary


class MatchPenaltyBreakdown(BaseModel):
    experience_penalty: float
    education_penalty: float
    total_penalty: float


class MatchScoreBreakdown(BaseModel):
    cosine_component: float
    skill_component: float
    domain_component: float
    seniority_component: float


class HardFilterResult(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class MatchResultItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    job_id: str
    source: str | None = None
    title: str
    apply_url: str
    locations: list[MatchLocationRead] = Field(default_factory=list)
    department: str | None = None
    team: str | None = None
    employment_type: str | None = None
    cosine_score: float
    skill_overlap_score: float
    domain_match_score: float
    seniority_match_score: float
    experience_gap: int
    education_gap: int
    penalties: MatchPenaltyBreakdown
    score_breakdown: MatchScoreBreakdown
    final_score: float
    hard_filter: HardFilterResult | None = None
    llm_recommendation: str | None = None
    llm_reasons: list[str] = Field(default_factory=list)
    llm_gaps: list[str] = Field(default_factory=list)
    llm_resume_focus_points: list[str] = Field(default_factory=list)
    llm_adjustment: float = 0.0
    llm_adjusted_score: float
    llm_enriched: bool = False


class MatchResponseMeta(BaseModel):
    user_json: str | None = None
    needs_sponsorship: bool
    user_total_years_experience: int | None = None
    user_degree_rank: int
    user_skill_count: int
    user_domain: str
    user_seniority: str | None = None
    top_k: int
    top_n: int
    sql_prefilter: SQLPrefilterSummary
    candidates_after_sql_prefilter: int
    vector_threshold_summary: VectorThresholdSummary
    candidates_after_vector_threshold: int
    candidates_before_hard_filter: int
    candidates_after_hard_filter: int
    hard_filter_summary: HardFilterSummary
    llm_rerank_summary: LLMRerankSummary
    results_returned: int


class MatchResponse(BaseModel):
    meta: MatchResponseMeta
    results: list[MatchResultItem]
