# JobX Master Plan: GCP Migration & Architecture

**Date:** 2026-03-06
**Status:** Draft for team review

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current State](#2-current-state)
3. [Architecture Options Analysis](#3-architecture-options-analysis)
4. [Recommended Architecture](#4-recommended-architecture)
5. [Firestore Data Model](#5-firestore-data-model)
6. [Vector Search Strategy](#6-vector-search-strategy)
7. [Ingest Pipeline Migration](#7-ingest-pipeline-migration)
8. [API & Serving Layer](#8-api--serving-layer)
9. [Blob Storage Migration](#9-blob-storage-migration)
10. [Orchestration & Scheduling](#10-orchestration--scheduling)
11. [VALET Integration](#11-valet-integration-future---do-not-implement-yet)
12. [Cost Analysis](#12-cost-analysis)
13. [Implementation Phases](#13-implementation-phases)
14. [Risk Register](#14-risk-register)
15. [Open Questions](#15-open-questions)

---

## 1. Executive Summary

**JobX** is a job aggregation and matching microservice that ingests jobs from 8+ ATS platforms (Greenhouse, Lever, Ashby, SmartRecruiters, etc.), normalizes them into a unified schema, enriches with LLM-extracted structured data, generates vector embeddings, and serves job recommendations via API.

**The goal:** Migrate JobX off the shared GHOST-HANDS Supabase instance onto its own GCP infrastructure, following the spirit of the v3.0 architecture plan — serverless, low-ops, auto-scaling, cost-efficient.

**Critical finding from research:** The v3.0 plan recommended Firestore + Vertex AI Vector Search. Our deep analysis revealed:

- **Vertex AI Vector Search costs ~$550/month minimum** (always-on node) — 98% of total cost, overkill for 200k vectors
- **Firestore lacks JOINs and inequality pre-filters with vector search**, making the current matching query (5-table JOIN + pgvector cosine sort + degree/sponsorship/location filters) impossible to replicate without major degradation
- **Firestore IS excellent** for the read-heavy serving layer that VALET needs

**Our recommendation:** A **hybrid architecture** — Firestore for the serving/read layer (cheap reads, auto-scaling, VALET-friendly) + a lightweight managed PostgreSQL for heavy processing (vector search, complex queries, batch sync) + Pinecone Serverless as an alternative to Vertex AI for vector search at 1/25th the cost.

---

## 2. Current State

### What exists today

| Component | Current Implementation | Status |
|-----------|----------------------|--------|
| **API** | FastAPI on localhost | Working |
| **Database** | Shared GHOST-HANDS Supabase (PostgreSQL) | No JobX tables created yet |
| **Vector Search** | pgvector extension | Code exists, not deployed |
| **Blob Storage** | Supabase Storage | Configured but pointing at GHOST-HANDS |
| **Orchestration** | Python script (`run_scheduled_ingests.py`) | Manual execution |
| **Deployment** | None (local only) | Dockerfile exists |

### Codebase inventory

| Layer | Files | Coupling to PostgreSQL |
|-------|-------|----------------------|
| **Fetchers** (ATS API clients) | 8 files | None (pure HTTP) |
| **Mappers** (normalization) | 8 files | None (pure transformation) |
| **Models** (SQLModel) | 6 files | Tight (SQLAlchemy decorators) |
| **Repositories** (data access) | 5 files | Tight (SQLAlchemy queries) |
| **Services** (business logic) | ~15 files | Medium (through repositories) |
| **Blob Storage** | 4 files | None (protocol-based abstraction) |

### Data scale

- ~235,000 jobs across ~5,200 sources
- 5 supported platforms currently active (ashby, greenhouse, lever, smartrecruiters, eightfold)
- ~10.77 KiB per job average
- ~2.5 GB total for job table
- Enrichment cost: ~$18 to parse + embed entire corpus

---

## 3. Architecture Options Analysis

### Option A: Firestore + Vertex AI Vector Search (v3.0 plan as-is)

| Pros | Cons |
|------|------|
| Fully serverless, zero-ops | Vertex AI Vector Search: **~$550/month minimum** |
| Firestore auto-scales reads | No JOINs in Firestore — matching query must be rewritten |
| Native GCP ecosystem | Firestore vector search limited (no inequality pre-filters) |
| Generous free tier for Firestore | 19+ weeks migration estimate |
| | Significant capability degradation in matching |

**Monthly cost estimate: ~$555**

### Option B: Firestore + Pinecone Serverless (modified v3.0)

| Pros | Cons |
|------|------|
| Firestore nearly free at this scale | Still requires matching query rewrite |
| Pinecone has generous free tier | Cross-service vector search (Pinecone) + metadata (Firestore) |
| Serverless end-to-end | Denormalization required (locations, sources embedded in jobs) |
| ~$7-26/month total | Application-level unique constraint enforcement |
| | Two external services to manage |

**Monthly cost estimate: ~$7-26**

### Option C: Cloud SQL PostgreSQL + pgvector (keep SQL, move off Supabase)

| Pros | Cons |
|------|------|
| Zero query rewrites | Cloud SQL: ~$50-60/month for smallest instance |
| Keeps pgvector, Alembic, SQLModel | Not fully serverless (always-on DB instance) |
| Matching query works as-is | Still need separate from GHOST-HANDS Supabase |
| Fastest migration (2-3 weeks) | Doesn't address Adam's concern about PG compute costs during batch writes |
| | |

**Monthly cost estimate: ~$60-80**

### Option D: Hybrid — Firestore (serving) + Cloud SQL (processing) [RECOMMENDED]

| Pros | Cons |
|------|------|
| Firestore handles cheap, fast reads for VALET | Two databases to maintain |
| Cloud SQL handles complex matching + batch writes | Sync logic between Firestore and Cloud SQL |
| Best of both: SQL power + NoSQL serving | More architectural complexity |
| Isolates batch write load from read path | |
| Matches Adam's vision: "Firebase for read-only is cheap" | |

**Monthly cost estimate: ~$55-75**

### Option E: Firestore + Firestore Native Vector Search (simplest Firestore path)

| Pros | Cons |
|------|------|
| Single database | Max 2048 dimensions (768 fits) |
| Built-in vector search | **No inequality pre-filters with vector search** |
| Cheapest option (~$2-5/month) | Max 1000 vector results per query |
| | Matching quality degraded (post-filter only) |
| | All denormalization challenges of Option B |

**Monthly cost estimate: ~$2-5**

---

## 4. Recommended Architecture

### Primary Recommendation: Option E (Firestore-only with native vector search)

After weighing all factors — Adam's stated preference for Firebase, cost sensitivity, the fact that this is an early-stage product, and the goal of "not reinventing the wheel" — **we recommend starting with Firestore-only (Option E)** for the following reasons:

1. **Cost**: ~$2-5/month vs $60+ for any PostgreSQL option
2. **Simplicity**: One database, one SDK, one billing account
3. **Adam's vision alignment**: Firestore for reads is exactly what he proposed
4. **Good enough matching**: Firestore native vector search works for 200k vectors. The limitation (no inequality pre-filters) can be worked around by post-filtering in the application layer — at 200k scale, fetching 1000 candidates and filtering down is fast enough
5. **Migration path**: If matching quality needs improvement later, we can add Pinecone (Option B) without changing the Firestore data layer

### Escape hatch

If Firestore's matching limitations become a real problem (not just theoretical), we upgrade to **Option B** (add Pinecone Serverless, ~$20/month more) or **Option D** (add Cloud SQL for processing, ~$50/month more). The Firestore serving layer stays either way.

### Architecture Diagram

See: [docs/architecture_diagram.html](architecture_diagram.html)

```
                    ARCHITECTURE OVERVIEW

    ATS APIs                    Cloud Scheduler
    (Greenhouse, Lever,         (daily cron)
     Ashby, SmartRecruiters,        |
     Eightfold, Apple,              v
     Uber, TikTok)          +------------------+
          |                 | Cloud Run Job    |
          v                 | (Ingest Script)  |
    +-----------+           +--------+---------+
    | Fetchers  |<---triggers--------|
    | (HTTP)    |                    |
    +-----------+                    |
          |                         |
          v                         v
    +-----------+           +------------------+
    | Mappers   |           | Pub/Sub Topic    |
    | (normalize)|          | (enrichment tasks)|
    +-----------+           +--------+---------+
          |                         |
          v                         v
    +-----------+           +------------------+
    | Firestore |<----------| Cloud Run Service|
    | (jobs,    |  writes   | (Enrichment      |
    |  sources, |           |  Workers)        |
    |  syncRuns)|           |  - LLM parsing   |
    |           |           |  - Embedding gen  |
    +-----------+           +------------------+
          |
          | reads
          v
    +------------------+
    | Cloud Run Service|
    | (FastAPI)        |
    | /api/v1/jobs     |       +----------+
    | /api/v1/matching |------>| VALET    |
    | /api/v1/sources  |       | (future) |
    +------------------+       +----------+

    +------------------+
    | GCS Bucket       |
    | (blob storage)   |
    | - HTML descs     |
    | - raw payloads   |
    +------------------+
```

---

## 5. Firestore Data Model

### Collection: `sources`

**Document ID:** `{platform}_{identifier}` (e.g., `greenhouse_stripe`)

```
{
  id: string (UUID, for backward compat),
  name: string,
  nameNormalized: string,
  platform: string (enum),
  identifier: string,
  enabled: boolean,
  notes: string | null,
  createdAt: timestamp,
  updatedAt: timestamp
}
```

**Why composite doc ID:** Enforces uniqueness of (platform, identifier) at the Firestore level — no application-level validation needed.

### Collection: `jobs`

**Document ID:** `{sourceDocId}_{externalJobId}` (e.g., `greenhouse_stripe_12345`)

This enforces the critical uniqueness constraint: one job per source + external ID. Writes are naturally idempotent (set = upsert).

```
{
  id: string (UUID, for API responses),
  sourceId: string (references sources doc ID),
  externalJobId: string,
  title: string,
  applyUrl: string,
  normalizedApplyUrl: string | null,
  contentFingerprint: string | null,
  status: string ("open" | "closed"),

  // Denormalized source info (avoids JOIN)
  sourcePlatform: string,
  sourceIdentifier: string,

  // Structured fields
  department: string | null,
  team: string | null,
  employmentType: string | null,
  descriptionPlain: string | null,

  // LLM-extracted fields (denormalized from structured_jd)
  sponsorshipNotAvailable: string ("yes" | "no" | "unknown"),
  jobDomainNormalized: string,
  minDegreeLevel: string,
  minDegreeRank: number (-1 to 4),
  experienceYears: number | null,
  seniorityLevel: string | null,
  structuredJdVersion: number,
  structuredJd: map {
    requiredSkills: array<string>,
    preferredSkills: array<string>,
    experienceRequirements: array<string>,
    educationRequirements: array<string>
  },

  // Denormalized locations (avoids JOIN table)
  locations: array<map> [
    {
      locationId: string,
      canonicalKey: string,
      displayName: string,
      city: string | null,
      region: string | null,
      countryCode: string | null,
      isPrimary: boolean,
      workplaceType: string,
      remoteScope: string | null
    }
  ],

  // Top-level country codes array (for array-contains queries)
  countryCodes: array<string> (e.g., ["US", "CA"]),

  // Blob storage pointers (GCS keys)
  descriptionHtmlKey: string | null,
  descriptionHtmlHash: string | null,
  rawPayloadKey: string | null,
  rawPayloadHash: string | null,

  // Vector embedding (for Firestore native vector search)
  embedding: vector(768) | null,
  embeddingModel: string | null,
  embeddingUpdatedAt: timestamp | null,

  // Timestamps
  publishedAt: timestamp | null,
  sourceUpdatedAt: timestamp | null,
  lastSeenAt: timestamp,
  createdAt: timestamp,
  updatedAt: timestamp
}
```

**Key design decisions:**
- **Locations denormalized**: Embedded as array in job document. Jobs rarely have >5 locations. Avoids the biggest Firestore pain point (no JOINs).
- **`countryCodes` array**: Separate top-level field for `array-contains` filtering (Firestore can't filter on nested array object fields).
- **Source info denormalized**: `sourcePlatform` and `sourceIdentifier` stored directly to avoid source lookup JOIN.
- **Embedding stored in document**: Uses Firestore's native vector type for `find_nearest` queries.
- **Structured JD fields at root level**: Key queryable fields (degree, sponsorship, domain) are denormalized to root for indexing.

### Collection: `locations` (reference collection)

**Document ID:** `{canonicalKey}` (e.g., `us_ca_san-francisco`)

```
{
  id: string (UUID),
  canonicalKey: string,
  displayName: string,
  city: string | null,
  region: string | null,
  countryCode: string | null,
  latitude: number | null,
  longitude: number | null,
  geonamesId: number | null,
  createdAt: timestamp,
  updatedAt: timestamp
}
```

Used as a reference/lookup collection. Location data is denormalized into job documents for query purposes.

### Collection: `syncRuns`

**Document ID:** auto-generated

```
{
  id: string (UUID),
  sourceId: string,
  status: string ("running" | "success" | "failed"),
  startedAt: timestamp,
  finishedAt: timestamp | null,
  stats: map {
    fetchedCount: number,
    mappedCount: number,
    uniqueCount: number,
    dedupedByExternalId: number,
    insertedCount: number,
    updatedCount: number,
    closedCount: number,
    failedCount: number
  },
  errorSummary: string | null,
  createdAt: timestamp
}
```

### Required Composite Indexes

```
1. jobs: (sourceId ASC, status ASC, lastSeenAt ASC)     — bulk close query
2. jobs: (status ASC, jobDomainNormalized ASC)           — filtered listing
3. jobs: (sourceId ASC, externalJobId ASC)               — dedup lookup (backup)
4. sources: (platform ASC, enabled ASC)                   — list enabled sources
5. syncRuns: (sourceId ASC, status ASC, startedAt DESC)  — overlap guard
```

Firestore limit: 200 composite indexes per database. We need ~5-10. Well within limit.

---

## 6. Vector Search Strategy

### Primary: Firestore Native Vector Search

Firestore supports `find_nearest` on vector fields up to 2048 dimensions. Our embeddings are 768 dimensions — this fits.

**How it works:**
```python
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

# Generate user embedding
user_embedding = await embed_text(user_profile_text)

# Vector search with pre-filters (equality only)
query = (
    db.collection("jobs")
    .where("status", "==", "open")
    .find_nearest(
        vector_field="embedding",
        query_vector=Vector(user_embedding),
        distance_measure=DistanceMeasure.COSINE,
        limit=200
    )
)
candidates = [doc.to_dict() async for doc in query.stream()]
```

**Limitations and workarounds:**

| Limitation | Impact | Workaround |
|-----------|--------|-----------|
| No inequality pre-filters with vector search | Can't do `minDegreeRank <= 3` in query | Post-filter: fetch 500 candidates, filter in Python |
| Max 1000 results | Can't get more than 1000 similar jobs | Sufficient — we only need top 50-200 |
| No JOINs | Can't join to locations table | Already solved: locations denormalized in job doc |
| Equality pre-filters only | Can filter `status == "open"` | Use for the most selective filter, post-filter rest |

**Matching query migration:**

Current SQL (simplified):
```sql
SELECT j.*, (1 - (je.embedding <=> user_vec)) AS cosine_score
FROM job j JOIN job_embedding je ON j.id = je.job_id
WHERE j.status = 'open'
  AND j.sponsorship_not_available <> 'yes'    -- inequality
  AND j.min_degree_rank <= user_degree_rank   -- inequality
  AND EXISTS (job_locations WHERE country = ?) -- JOIN
ORDER BY cosine_score DESC LIMIT 200
```

Firestore equivalent (in application code):
```python
# Step 1: Vector search with equality pre-filter
candidates = firestore.collection("jobs") \
    .where("status", "==", "open") \
    .find_nearest(embedding, limit=500)

# Step 2: Post-filter in Python (fast at this scale)
filtered = [
    c for c in candidates
    if (not needs_sponsorship or c["sponsorshipNotAvailable"] != "yes")
    and (c["minDegreeRank"] <= user_degree_rank or c["minDegreeRank"] < 0)
    and (not preferred_country or preferred_country in c.get("countryCodes", []))
]

# Step 3: Score and rank (same logic as current rerank_match_candidates)
scored = score_and_rank(filtered, user_profile)
return scored[:top_n]
```

**Performance expectation:** At 200k jobs, fetching 500 vector candidates and post-filtering ~100 in Python takes <100ms. This is acceptable for a recommendation API.

### Escape hatch: Pinecone Serverless

If Firestore native vector search proves insufficient (poor recall, too slow, need filtered search), add Pinecone:

- Free tier: 2M read units, 5GB storage (covers our needs)
- Supports filtered vector search (metadata filters with inequalities)
- Cost: ~$0-20/month for our scale
- Integration: store vectors in Pinecone, fetch job docs from Firestore by ID

This is additive — doesn't require changing the Firestore data layer.

---

## 7. Ingest Pipeline Migration

### What stays the same (zero changes)

| Component | Why |
|-----------|-----|
| All 8 fetchers | Pure HTTP, no DB dependency |
| All 8 mappers | Pure transformation logic |
| Deduplication logic | In-memory dict keying |
| Location parsing & GeoNames resolution | Domain logic, DB-agnostic |
| Retry logic (tenacity) | Infrastructure-agnostic |
| Statistics tracking | Dataclass accumulation |

### What changes

#### Repository layer → Firestore SDK

**Current:** `JobRepository` uses SQLAlchemy async queries
**New:** `FirestoreJobRepository` uses `firebase-admin` async client

Key method translations:

| Method | Current (SQL) | New (Firestore) |
|--------|---------------|-----------------|
| `get_by_id(id)` | `select(Job).where(Job.id == id)` | `db.collection("jobs").document(id).get()` |
| `list_by_source_id_and_external_ids()` | `WHERE source_id = ? AND external_job_id IN (...)` | `db.collection("jobs").where("sourceId", "==", src).where("externalJobId", "in", ids)` (batched, max 30 per `in`) |
| `save_all_no_commit()` | `session.add(job)` + `session.flush()` | `batch.set(doc_ref, job_data)` (max 500 per batch) |
| `bulk_close_missing_for_source_id()` | `UPDATE jobs SET status='closed' WHERE source_id=? AND last_seen_at < ?` | Query + batch update (paginated, 500 per batch) |

#### Transaction model

**Current:** Single SQLAlchemy transaction wrapping all stages, commit at end, rollback on error.

**New:** Firestore batched writes (max 500 per batch). The pipeline becomes:

1. Fetch + Map + Dedupe (unchanged)
2. Build existing map (Firestore query)
3. Stage jobs (build dicts, not SQLModel objects)
4. Batch write jobs to Firestore (500 at a time)
5. Batch write location references
6. Batch close stale jobs (query + update)
7. Write SyncRun record

**Atomicity trade-off:** Firestore batches are atomic within 500 writes, but we can't wrap the entire sync in one transaction. This is acceptable — if a sync fails mid-way, the next sync will reconcile (full-snapshot pattern handles this naturally).

#### Blob sync → GCS

**Current:** `SupabaseBlobStorage` uploads gzipped blobs via REST API
**New:** `GCSBlobStorage` uploads via `google-cloud-storage` SDK

The `BlobStorageClient` protocol is already abstracted — this is a drop-in replacement.

---

## 8. API & Serving Layer

### FastAPI on Cloud Run

The existing Dockerfile is already Cloud Run compatible:
- Uses `python:3.12-slim` base
- Exposes port 8080, respects `PORT` env var
- Runs uvicorn

**Deployment:**
```bash
gcloud run deploy jobx-api \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "GCP_PROJECT=your-project"
```

**Configuration:**
- Min instances: 0 (scale to zero when idle, save cost)
- Max instances: 10 (cap for safety)
- Concurrency: 80 (default, good for async FastAPI)
- Memory: 512Mi
- CPU: 1

**Cold start mitigation:** For the API service, cold starts of 1-2s are acceptable for a recommendation API. If latency matters, set `min-instances=1` (~$15/month additional).

### API endpoints (unchanged)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /health` | GET | Health check + read_only_mode status |
| `GET /api/v1/jobs` | GET | List jobs (paginated, filtered) |
| `GET /api/v1/jobs/{id}` | GET | Get single job with blob content |
| `POST /api/v1/jobs` | POST | Create job |
| `PATCH /api/v1/jobs/{id}` | PATCH | Update job |
| `DELETE /api/v1/jobs/{id}` | DELETE | Delete job |
| `GET /api/v1/sources` | GET | List sources |
| `POST /api/v1/sources` | POST | Create source |
| `POST /api/v1/matching/recommendations` | POST | Get job recommendations |

---

## 9. Blob Storage Migration

### Current: Supabase Storage

- HTML descriptions: `job-html/{sha256}.html.gz`
- Raw payloads: `job-raw/{sha256}.json.gz`
- Content-addressed (keyed by SHA-256 hash)
- Gzip compressed before upload

### New: Google Cloud Storage (GCS)

Same key scheme, same compression. New implementation of `BlobStorageClient` protocol:

```python
class GCSBlobStorage:
    def __init__(self, bucket_name: str):
        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket_name)

    async def upload_if_missing(self, key, data, content_type, content_encoding="gzip"):
        blob = self.bucket.blob(key)
        if blob.exists():
            return False
        blob.upload_from_string(data, content_type=content_type)
        return True

    async def download(self, key):
        blob = self.bucket.blob(key)
        return blob.download_as_bytes()
```

**Cost:** GCS Standard storage: $0.020/GB/month. For ~10GB of blobs: ~$0.20/month.

---

## 10. Orchestration & Scheduling

### Current: Manual Python script

`run_scheduled_ingests.py` is run manually or via external cron.

### New: Cloud Scheduler + Cloud Run Jobs

**Phase 1 (simplest):**
```
Cloud Scheduler (cron: "0 6 * * *")
    → triggers Cloud Run Job
    → runs run_scheduled_ingests.py
    → exits when done
```

The existing script barely needs changes:
- Remove interactive prompt (not needed in automated runs)
- Add `--yes` flag by default
- Environment variables provided by Cloud Run

**Phase 2 (per-source parallelism, future):**
```
Cloud Scheduler
    → Cloud Workflows (YAML)
    → For each enabled source (parallel):
        → Pub/Sub message
        → Cloud Run Service processes one source
```

### Async Enrichment Pipeline

For LLM parsing and embedding generation, use Pub/Sub + Cloud Run:

```
Ingest Job completes
    → publishes job IDs to Pub/Sub topic "jobs-to-enrich"
    → Cloud Run Service subscribes
    → For each job:
        1. Fetch description from GCS
        2. Call LLM for structured JD extraction
        3. Generate embedding via SiliconFlow API
        4. Update Firestore document with structured_jd + embedding
```

**Why separate:** Enrichment is the expensive, slow part (LLM calls). Decoupling it from ingest means:
- Ingest is fast (just fetch + normalize + write metadata)
- Enrichment can retry independently
- Enrichment can scale independently (more Cloud Run instances)

---

## 11. VALET Integration (FUTURE - DO NOT IMPLEMENT YET)

> **NOTE:** This section documents how VALET will eventually consume JobX data. DO NOT implement this yet. Focus on getting JobX working independently first.

### VALET Architecture Summary

- **Tech stack:** TypeScript, Fastify 5.x, React 18, Drizzle ORM
- **Database:** PostgreSQL on Supabase (shared with GHOST-HANDS)
- **Current job data:** User-submitted job leads only (no recommendations)
- **No Firestore dependency** exists in VALET today

### How VALET will consume JobX

**Option A: Direct API call (simplest)**
```
VALET Backend → HTTP → JobX API (Cloud Run)
    POST /api/v1/matching/recommendations
    Body: { user profile, skills, preferences }
    Response: { ranked job list with scores }
```

VALET stores matches as job leads:
```typescript
// VALET creates a job_lead from JobX recommendation
const jobLead = {
  userId: user.id,
  jobUrl: recommendation.applyUrl,    // From JobX
  platform: recommendation.sourcePlatform,
  title: recommendation.title,
  company: recommendation.sourceIdentifier,
  location: recommendation.locations[0]?.displayName,
  matchScore: recommendation.finalScore,
  source: "jobx_recommendation",
  status: "saved"
};
```

**Option B: Shared Firestore (future optimization)**

If VALET needs to browse/filter the full job corpus (not just get recommendations), it could read directly from the same Firestore database. Firestore's IAM can restrict VALET to read-only access.

### Data VALET needs from each job

| Field | Required | Purpose |
|-------|----------|---------|
| `applyUrl` | Yes | Direct link for GhostHands to navigate |
| `title` | Yes | Display in dashboard |
| `sourceIdentifier` | Yes | Company name proxy |
| `sourcePlatform` | Yes | "Found on Greenhouse" UX |
| `locations` | Yes | Display + filtering |
| `employmentType` | Nice to have | Display |
| `descriptionPlain` | Nice to have | Preview in dashboard |
| `matchScore` | Yes (from matching API) | Ranking in recommendations |
| `structuredJd` | Nice to have | Show requirements |

---

## 12. Cost Analysis

### Monthly cost comparison

| Component | Current (Supabase shared) | Option E (Firestore) | Option C (Cloud SQL) |
|-----------|--------------------------|---------------------|---------------------|
| **Database** | $0 (shared) | ~$1-2 | ~$50-60 |
| **Vector search** | pgvector (included) | Firestore native (included) | pgvector (included) |
| **Blob storage** | Supabase Storage | GCS: ~$0.20 | GCS: ~$0.20 |
| **API hosting** | Local | Cloud Run: ~$0-5 | Cloud Run: ~$0-5 |
| **Batch processing** | Local | Cloud Run Jobs: ~$2-5 | Cloud Run Jobs: ~$2-5 |
| **Pub/Sub** | N/A | ~$0 (free tier) | ~$0 (free tier) |
| **Scheduler** | Manual | Cloud Scheduler: ~$0.10 | Cloud Scheduler: ~$0.10 |
| **LLM enrichment** | SiliconFlow API | SiliconFlow: ~$18/full run | Same |
| **TOTAL** | **$0** (but uses shared DB) | **~$5-10/month** | **~$55-75/month** |

### Cost per operation (Firestore)

| Operation | Volume/month | Unit cost | Monthly cost |
|-----------|-------------|-----------|-------------|
| Writes (daily sync ~5k jobs) | 150k | $0.18/100k | $0.27 |
| Reads (API queries) | 500k | $0.06/100k | $0.30 |
| Storage (200k docs @ ~2KB) | 400MB + indexes | $0.18/GB | $0.14 |
| Vector search reads | 50k | $0.06/100k | $0.03 |
| **Total Firestore** | | | **~$0.74** |

---

## 13. Implementation Phases

### Phase 0: Local Development Setup (Week 1)

**Goal:** Get JobX running locally against a local Firestore emulator.

- [ ] Install Firebase CLI and Firestore emulator
- [ ] Create `FirestoreJobRepository`, `FirestoreSourceRepository`, `FirestoreLocationRepository`
- [ ] Implement `GCSBlobStorage` (or use local filesystem for dev)
- [ ] Adapt `FullSnapshotSyncService` to use Firestore repositories
- [ ] Run a single-source ingest locally (e.g., `--identifier stripe --platform greenhouse`)
- [ ] Verify data in Firestore emulator UI

**Deliverable:** Single-source end-to-end ingest working against Firestore emulator.

### Phase 1: Core Pipeline MVP (Week 2-3)

**Goal:** Deploy to GCP, run daily ingest for a handful of sources.

- [ ] Create GCP project + enable APIs (Firestore, Cloud Run, GCS, Cloud Scheduler)
- [ ] Create Firestore database (Native mode)
- [ ] Create GCS bucket for blobs
- [ ] Deploy FastAPI to Cloud Run
- [ ] Deploy ingest script as Cloud Run Job
- [ ] Set up Cloud Scheduler for daily trigger
- [ ] Configure 5-10 test sources
- [ ] Verify: jobs appear in Firestore, API returns them

**Deliverable:** Daily automated ingest of 5-10 sources, browsable via API.

### Phase 2: Enrichment Pipeline (Week 3-4)

**Goal:** Add async LLM parsing and embedding generation.

- [ ] Create Pub/Sub topic `jobs-to-enrich`
- [ ] Modify ingest to publish job IDs after successful sync
- [ ] Create enrichment Cloud Run Service (subscribes to Pub/Sub):
  - Structured JD extraction via LLM
  - Embedding generation via SiliconFlow
  - Update Firestore job document with results
- [ ] Backfill existing jobs
- [ ] Verify: jobs have `structuredJd` and `embedding` fields populated

**Deliverable:** All ingested jobs get LLM-parsed structured data and vector embeddings.

### Phase 3: Matching API (Week 5-6)

**Goal:** Implement the recommendation endpoint using Firestore vector search.

- [ ] Implement `POST /api/v1/matching/recommendations`:
  - Accept user profile (skills, experience, preferences)
  - Generate user embedding
  - Firestore `find_nearest` with `status == "open"` pre-filter
  - Post-filter for sponsorship, degree, country
  - Score and rank (cosine + skill overlap + domain match)
  - Return top N with explanations
- [ ] Test with sample user profiles
- [ ] Benchmark latency (target: <2s for recommendation query)

**Deliverable:** Working recommendation API.

### Phase 4: Scale Up (Week 7-8)

**Goal:** Onboard all ~5,200 sources, validate at full scale.

- [ ] Gradually increase `INGEST_MAX_SOURCES` (50 → 200 → 1000 → all)
- [ ] Monitor Firestore costs, Cloud Run scaling, Pub/Sub throughput
- [ ] Verify matching quality at 200k+ jobs
- [ ] Tune enrichment concurrency and batch sizes
- [ ] Set up monitoring/alerting (Cloud Monitoring)
- [ ] Document operational runbook

**Deliverable:** Full-scale production system with all sources.

### Phase 5: VALET Integration (Future — NOT IN SCOPE)

- [ ] VALET team adds HTTP client for JobX recommendation API
- [ ] VALET surfaces recommendations in dashboard
- [ ] User clicks "Apply" → creates job lead → queues GhostHands task

---

## 14. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Firestore vector search quality insufficient | Medium | High | Escape hatch: add Pinecone Serverless (~$20/month) |
| Firestore 500-write batch limit causes issues during large syncs | Medium | Medium | Paginate batches, accept eventual consistency |
| Post-filtering 500 candidates is too slow | Low | Medium | At 200k scale with Python, filtering 500 dicts takes <10ms |
| Firestore costs spike unexpectedly | Low | Medium | Set budget alerts, monitor read/write counts |
| Enrichment Pub/Sub backlog grows | Medium | Low | Auto-scaling Cloud Run handles bursts; set max instances |
| Denormalized location data gets stale | Low | Low | Locations rarely change; re-sync on next ingest |
| Firebase SDK async support issues | Low | Medium | Fallback to sync SDK with thread pool executor |

---

## 15. Open Questions

1. **GCP Project:** Do we use an existing GCP project or create a new one? (Affects billing, IAM)

2. **Firestore location:** Which GCP region? (Should match Cloud Run region for latency. Recommendation: `us-central1` for cost)

3. **Source of truth:** When VALET integration happens, is Firestore the source of truth for jobs, or does VALET maintain its own copy in Supabase?

4. **Embedding model:** The current config uses `Qwen/Qwen3-Embedding-0.6B` via SiliconFlow. Should we switch to Google's `text-embedding-004` for tighter GCP integration? (Firestore vector search works with any model)

5. **Who manages GCP?** DevOps setup, billing, IAM roles — does Adam's team handle this or do we?

6. **Domain/auth for API:** Does the Cloud Run API need authentication (API key, Firebase Auth, etc.) or is it internal-only?
