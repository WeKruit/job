# JobX Onboarding Guide

## Overview

JobX ingests job postings from public career pages (Greenhouse, Lever, Ashby, etc.), normalizes them into a standard schema, enriches them with LLM-parsed structured data + vector embeddings, and serves **job match recommendations** via API. All data lives in **Firestore**.

JobX is **stateless from the user's perspective** — it does not store any user data. The caller (VALET) sends a candidate profile, gets back ranked job matches, and is responsible for tracking what the user has seen/applied to/saved.

---

## Firestore Collections

The database has 6 collections:

### `sources`
Companies you want to track. Each document represents one company on one platform.

| Field | Description |
|-------|-------------|
| `name` | Display name (e.g. "Anthropic") |
| `name_normalized` | Lowercase name for dedup lookups |
| `platform` | Which ATS platform (`greenhouse`, `lever`, `ashby`, etc.) |
| `identifier` | The company's slug on that platform (e.g. `anthropic`) |
| `enabled` | Whether this source is included in scheduled ingests |
| `notes` | Optional freeform notes |
| `created_at` / `updated_at` | Timestamps |

### `jobs`
The core data. Every job posting that has been ingested lives here.

| Field | Description |
|-------|-------------|
| `source_id` | Links back to the source that owns this job |
| `external_job_id` | The job's ID on the original platform (used for dedup) |
| `title` | Job title |
| `department` | Department or team name |
| `apply_url` | Direct link to the application page |
| `description_plain` | Plain-text version of the job description |
| `status` | `open` or `closed` — closed means the job disappeared from the board |
| `last_seen_at` | Last time this job appeared in a fetch |
| `location_raw` | The raw location string from the platform |
| `structured_jd` | LLM-parsed structured job description (skills, experience_years, etc.) |
| `structured_jd_version` | Version of the JD parsing prompt that produced the structured_jd |
| `embedding` | 1024-dimensional vector (Firestore Vector type) for similarity search |
| `embedding_model` | Which model produced the embedding |
| `embedding_updated_at` | When the embedding was last generated |
| `created_at` / `updated_at` | Timestamps |

### `job_embeddings`
Dedicated embedding storage (one per job). Holds the same vector as `jobs.embedding` plus metadata about the embedding target/revision. Used by the embedding refresh service.

| Field | Description |
|-------|-------------|
| `job_id` | References a document in `jobs` |
| `embedding_kind` | Type of embedding (e.g. `jd_structured`) |
| `embedding_target_revision` | Version of the embedding target text |
| `embedding_model` | Model name |
| `embedding_dim` | Dimension count (1024) |
| `embedding` | The vector itself |
| `created_at` / `updated_at` | Timestamps |

### `locations`
Normalized location records. Multiple jobs can share the same location.

| Field | Description |
|-------|-------------|
| `canonical_key` | Unique key like `us-ca-san-francisco` or `in-bangalore` |
| `display_name` | Human-readable name (e.g. "San Francisco, CA, US") |
| `country_code` | ISO country code (e.g. `US`, `IN`) |
| `region` | State/province |
| `city` | City name |
| `is_remote` | Whether this is a remote position |

### `job_locations`
Join collection linking jobs to locations (a job can have multiple locations).

| Field | Description |
|-------|-------------|
| `job_id` | References a document in `jobs` |
| `location_id` | References a document in `locations` |
| `is_primary` | Whether this is the job's primary location |
| `source_raw` | The raw location string from the platform |
| `workplace_type` | e.g. `onsite`, `remote`, `hybrid` |
| `remote_scope` | e.g. `global`, `us_only` |

### `sync_runs`
Audit log of every ingest run. Useful for debugging and monitoring.

| Field | Description |
|-------|-------------|
| `source_id` | Which source was synced |
| `status` | `running`, `success`, or `failed` |
| `started_at` / `finished_at` | Timing |
| `fetched_count` | Jobs pulled from the API |
| `mapped_count` | Jobs after normalization |
| `unique_count` | Jobs after dedup |
| `inserted_count` | New jobs created |
| `updated_count` | Existing jobs refreshed |
| `closed_count` | Jobs marked closed (removed from the board) |
| `error_summary` | Error message if the run failed |

---

## Running the Ingest Pipeline

### Basic Command

```bash
uv run python -m scripts.run_scheduled_ingests [OPTIONS]
```

### All Options

| Flag | Default | Description |
|------|---------|-------------|
| `--platform` | all | Filter to one platform: `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `eightfold`, `apple`, `uber`, `tiktok` |
| `--identifier` | all | Exact company identifier (e.g. `anthropic`, `stripe`) |
| `--limit N` | 5 (capped) | Max number of sources to process. Hard-capped by `INGEST_MAX_SOURCES` in `.env` |
| `--include-content` / `--no-include-content` | `--include-content` | Fetch full job descriptions. Use `--no-include-content` for faster metadata-only sync |
| `--dry-run` | off | Fetches and maps jobs but does NOT write to Firestore |
| `--retry-attempts N` | 3 | Number of retry attempts per source on failure |
| `--yes` / `-y` | off | Skip the "are you sure?" confirmation prompt |

### Examples

**Ingest one company:**
```bash
uv run python -m scripts.run_scheduled_ingests --platform greenhouse --identifier anthropic --yes
```

**Dry-run first (no writes):**
```bash
uv run python -m scripts.run_scheduled_ingests --platform greenhouse --identifier anthropic --dry-run --yes
```

**Ingest all Greenhouse sources:**
```bash
uv run python -m scripts.run_scheduled_ingests --platform greenhouse --yes
```

**Ingest everything enabled:**
```bash
uv run python -m scripts.run_scheduled_ingests --yes
```

**Raise the source limit:**
```bash
uv run python -m scripts.run_scheduled_ingests --limit 20 --yes
```

> Note: `--limit` is capped by `INGEST_MAX_SOURCES` in `.env` (default 5). Change that value to allow larger runs.

---

## Running the Enrichment Pipeline

After ingesting jobs, you need to enrich them (parse JDs + generate embeddings) before matching will work.

### Basic Command

```bash
uv run python -m scripts.run_enrichment [OPTIONS]
```

### All Options

| Flag | Default | Description |
|------|---------|-------------|
| `--jd-batch-size N` | 10 | Jobs per LLM batch for JD parsing |
| `--jd-limit N` | all pending | Max total jobs to parse JDs for |
| `--version-only` | off | Only re-parse jobs with outdated `structured_jd_version` |
| `--skip-jd` | off | Skip JD parsing, only generate embeddings |
| `--skip-embeddings` | off | Skip embedding generation, only parse JDs |
| `--continue-on-error` | off | Continue processing after a batch failure (useful for rate limit issues) |

### Examples

**Full enrichment (JD parsing + embeddings):**
```bash
uv run python -m scripts.run_enrichment --continue-on-error
```

**Only generate embeddings (JDs already parsed):**
```bash
uv run python -m scripts.run_enrichment --skip-jd
```

**Only parse JDs (small batch):**
```bash
uv run python -m scripts.run_enrichment --skip-embeddings --jd-limit 50 --jd-batch-size 5
```

### Rate Limiting

The Gemini API has tokens-per-minute (TPM) limits. If you hit rate limits during JD parsing, use `--continue-on-error` and run the command multiple times — it only processes jobs that haven't been parsed yet.

---

## Running the Matching API

### Start the Server

```bash
uv run uvicorn app.main:app --reload --port 8000
```

API docs available at: `http://localhost:8000/docs` (Swagger UI)

### Matching Endpoint

**`POST /api/v1/matching/recommendations`**

Takes a candidate profile, returns ranked job recommendations.

### Request Body

```json
{
  "candidate": {
    "summary": "Software engineer with 3 years Python experience",
    "skills": ["Python", "AWS", "Docker", "PostgreSQL"],
    "workAuthorization": "us_citizen",
    "totalYearsExperience": 3,
    "education": [
      {
        "degree": "Bachelor of Science",
        "school": "UC Berkeley",
        "fieldOfStudy": "Computer Science"
      }
    ],
    "workHistory": [
      {
        "title": "Software Engineer",
        "company": "Startup Inc",
        "bullets": ["Built REST APIs serving 10k req/sec using Python FastAPI"],
        "description": "Backend engineering role",
        "achievements": ["Reduced API latency by 40%"]
      }
    ]
  },
  "top_k": 50,
  "top_n": 10,
  "min_cosine_score": 0.3,
  "enable_llm_rerank": false,
  "excludeJobIds": [],
  "preferredCountryCode": "US",
  "needs_sponsorship_override": "auto",
  "experience_buffer_years": 1
}
```

### Request Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `candidate` | object | **required** | The user's profile (see CandidateProfile below) |
| `top_k` | int | 200 | How many candidates to retrieve from vector search |
| `top_n` | int | 50 | How many final results to return |
| `min_cosine_score` | float | 0.48 | Minimum cosine similarity threshold (0.0-1.0) |
| `needs_sponsorship_override` | string | "auto" | `"auto"`, `"true"`, or `"false"` — override sponsorship detection |
| `experience_buffer_years` | int | 1 | How many years of experience gap to tolerate |
| `enable_llm_rerank` | bool | false | Enable LLM-based reranking (requires Gemini API key) |
| `llm_top_n` | int | 10 | How many top results to send to LLM rerank |
| `llm_concurrency` | int | 3 | Concurrent LLM rerank requests |
| `max_user_chars` | int | 12000 | Max chars of user text to embed |
| `preferredCountryCode` | string | null | ISO country code (e.g. "US") for location preference |
| `excludeJobIds` | list[str] | [] | Job IDs to exclude (already applied/saved/recommended) |
| `user_json` | string | null | Raw JSON string echoed back in response meta (for debugging) |

### CandidateProfile Fields

| Field | Type | Description |
|-------|------|-------------|
| `summary` | string | Free-text summary of the candidate |
| `skills` | list[str] | List of skill keywords |
| `workAuthorization` | string | e.g. `"us_citizen"`, `"h1b"`, `"opt"` |
| `totalYearsExperience` | int | Total years of work experience |
| `education` | list | Array of `{degree, school, fieldOfStudy}` |
| `workHistory` | list | Array of `{title, company, bullets, description, achievements}` |

### Response Shape

```json
{
  "meta": {
    "needs_sponsorship": false,
    "user_total_years_experience": 3,
    "user_degree_rank": 2,
    "user_skill_count": 4,
    "user_domain": "software_engineering",
    "user_seniority": "mid",
    "top_k": 50,
    "top_n": 10,
    "candidates_after_sql_prefilter": 50,
    "candidates_after_vector_threshold": 45,
    "candidates_after_hard_filter": 22,
    "results_returned": 10
  },
  "results": [
    {
      "job_id": "abc123",
      "source": "greenhouse:anthropic",
      "title": "Software Engineer, Backend",
      "apply_url": "https://boards.greenhouse.io/anthropic/jobs/123",
      "locations": [
        {
          "city": "San Francisco",
          "region": "CA",
          "country_code": "US",
          "display_name": "San Francisco, CA, US",
          "is_primary": true,
          "workplace_type": "onsite"
        }
      ],
      "department": "Engineering",
      "team": "Platform",
      "employment_type": "full_time",
      "cosine_score": 0.72,
      "skill_overlap_score": 0.5,
      "domain_match_score": 1.0,
      "seniority_match_score": 0.8,
      "experience_gap": 0,
      "education_gap": 0,
      "penalties": {
        "experience_penalty": 0.0,
        "education_penalty": 0.0,
        "total_penalty": 0.0
      },
      "score_breakdown": {
        "cosine_component": 0.504,
        "skill_component": 0.075,
        "domain_component": 0.1,
        "seniority_component": 0.04
      },
      "final_score": 0.719,
      "llm_adjusted_score": 0.719,
      "llm_enriched": false
    }
  ]
}
```

### Matching Pipeline Stages

1. **Embed user profile** — Converts candidate text to a 1024-dim vector via SiliconFlow API
2. **Vector recall** — Firestore `find_nearest` retrieves `top_k` most similar jobs (status=open only)
3. **Cosine threshold** — Drops jobs below `min_cosine_score`
4. **Hard filters** — Rejects on sponsorship, experience gap, degree requirements
5. **Deterministic rerank** — Computes `final_score = 0.70*cosine + 0.15*skill_overlap + 0.10*domain + 0.05*seniority - penalties`
6. **Optional LLM rerank** — If `enable_llm_rerank=true`, sends top candidates to Gemini for evaluation
7. **Return top_n** — Final results with full score breakdowns

---

## Adding a New Company

Before ingesting a company, it must exist as a **source** in Firestore.

### Option 1: Via the API

```
POST /api/v1/sources/
{
  "name": "Stripe",
  "platform": "greenhouse",
  "identifier": "stripe"
}
```

### Option 2: Quick script

```bash
uv run python -c "
import asyncio
from app.infrastructure.firestore_client import get_firestore_client
from app.repositories.firestore import FirestoreSourceRepository
from app.models import Source, PlatformType

async def add():
    db = get_firestore_client()
    repo = FirestoreSourceRepository(db)
    source = Source(
        name='Stripe',
        platform=PlatformType.GREENHOUSE,
        identifier='stripe',
        enabled=True,
    )
    await repo.create(source)
    print(f'Created: {source.name} ({source.platform.value}:{source.identifier})')

asyncio.run(add())
"
```

Then run the ingest + enrichment:
```bash
uv run python -m scripts.run_scheduled_ingests --platform greenhouse --identifier stripe --yes
uv run python -m scripts.run_enrichment --continue-on-error
```

---

## What is the "identifier"?

The identifier is the company's slug on the platform's public job board URL:

| Platform | URL Pattern | Example identifier |
|----------|------------|-------------------|
| Greenhouse | `https://boards.greenhouse.io/{id}` | `anthropic`, `stripe`, `figma` |
| Lever | `https://jobs.lever.co/{id}` | `netflix`, `twitch` |
| Ashby | `https://jobs.ashbyhq.com/{id}` | `notion` |
| SmartRecruiters | SmartRecruiters company ID | varies |
| Apple / Uber / TikTok | Hardcoded fetchers | any string works |

---

## Environment Variables

Key settings in your `.env` file:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FIRESTORE_CREDENTIALS_FILE` | Yes | — | Path to Firebase service account JSON. Switches from Postgres to Firestore |
| `SILICONFLOW_API_KEY` | Yes (for embeddings) | — | API key for SiliconFlow embedding service |
| `GEMINI_API_KEY` | Yes (for JD parsing) | — | Google Gemini API key for LLM-based JD parsing |
| `EMBEDDING_PROVIDER` | No | `gemini` | Which embedding provider to use |
| `EMBEDDING_API_KEY` | No | falls back to `GEMINI_API_KEY` | Embedding API key (overrides default) |
| `EMBEDDING_API_BASE` | No | — | Custom base URL for embedding API |
| `EMBEDDING_MODEL` | No | `gemini-embedding-001` | Embedding model name |
| `EMBEDDING_DIM` | No | `768` | Embedding dimensions (actual model may return 1024) |
| `LLM_PROVIDER` | No | `gemini` | LLM provider for JD parsing |
| `LLM_MODEL` | No | `gemini-2.5-flash-lite` | LLM model for JD parsing |
| `LLM_API_KEY` | No | falls back to `GEMINI_API_KEY` | LLM API key (overrides default) |
| `INGEST_MAX_SOURCES` | No | `5` | Safety cap on sources per ingest run |
| `READ_ONLY_MODE` | No | `true` | Blocks DB writes via API when true |
| `APP_ENV` | No | `development` | App environment |
| `DEBUG` | No | `false` | Debug mode |

---

## What Happens During an Ingest

1. **Fetch** — Pulls all open jobs from the platform's public API
2. **Map** — Normalizes raw API data into the standard Job model
3. **Dedupe** — Removes duplicate `external_job_id` entries within the batch
4. **Stage** — Compares against existing Firestore jobs: new ones get inserted, existing ones get updated
5. **Location sync** — Parses location strings into structured location records, creates/links them
6. **Finalize** — Marks any jobs not seen in this fetch as `closed` (removed from the board)
7. **Embedding refresh** — Generates embeddings for any new/updated jobs (runs automatically during ingest)

### Reading the Output

```
fetched=450        # Jobs pulled from the API
unique=450         # After deduplication
inserted=450       # New jobs created in Firestore
updated=0          # Existing jobs refreshed with latest data
closed=0           # Jobs marked closed (no longer on the board)
```

On subsequent runs for the same source, you'll typically see `inserted=0` and `updated=N` since the jobs already exist and are just being refreshed.

---

## Supported Platforms

| Platform | Status |
|----------|--------|
| Greenhouse | Supported |
| Lever | Supported |
| Ashby | Supported |
| SmartRecruiters | Supported |
| Eightfold | Supported |
| Apple | Supported |
| Uber | Supported |
| TikTok | Supported |
| Workday | Not yet supported |
| GitHub Jobs | Not yet supported |

---

## Current Data State (as of 2026-03-08)

- **1 source active**: Anthropic (Greenhouse)
- **450 jobs** ingested (all open)
- **425 jobs** have embeddings (25 missing — likely have no description text)
- **~163 jobs** have full structured JD parsing (~280 failed due to Gemini rate limits, re-run enrichment to complete)
- **Vector index**: Composite index on `status + embedding` (1024-dim, COSINE, flat) — status: READY

To complete JD parsing for remaining jobs:
```bash
uv run python -m scripts.run_enrichment --skip-embeddings --continue-on-error
```
