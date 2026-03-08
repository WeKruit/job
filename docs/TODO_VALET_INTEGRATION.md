# VALET Integration with JobX — Implementation TODO

**Context:** JobX is a job aggregation + matching microservice. It exposes a single matching API endpoint that takes a candidate profile and returns ranked job recommendations. VALET needs to integrate with this API to show job recommendations to users.

**JobX repo:** `C:\Users\spenc\Documents\GitHub\job`
**VALET repo:** `C:\Users\spenc\Documents\GitHub\VALET`

---

## What JobX Provides (already done)

- `POST /api/v1/matching/recommendations` — takes a CandidateProfile, returns ranked job matches
- Firestore-backed vector search over ~450 Anthropic jobs (more companies can be added)
- `excludeJobIds` parameter to prevent re-recommending jobs the user has already seen
- Full score breakdowns (cosine similarity, skill overlap, domain match, seniority match, penalties)
- Each result includes: job_id, title, apply_url, locations, department, team, employment_type, all scores

**JobX is stateless** — it does not store any user data. All user state (profile, seen jobs, saved jobs) must be managed by VALET.

---

## Tasks for VALET

### 1. Build CandidateProfile from parsed resume

VALET already has resume parsing (via GHOST-HANDS or its own parser). Map the parsed resume data into the JobX `CandidateProfile` schema:

```typescript
interface CandidateProfile {
  summary?: string;               // Free-text summary
  skills: string[];               // Skill keywords
  workAuthorization?: string;     // "us_citizen", "h1b", "opt", "green_card", etc.
  totalYearsExperience?: number;  // Total years of work experience
  education: Array<{
    degree?: string;              // "Bachelor of Science", "Master of Arts", etc.
    school?: string;
    fieldOfStudy?: string;        // "Computer Science", "Mechanical Engineering", etc.
  }>;
  workHistory: Array<{
    title?: string;               // "Software Engineer", "Product Manager", etc.
    company?: string;
    bullets: string[];            // Bullet points from resume
    description?: string;         // Role description
    achievements: string[];       // Key achievements
  }>;
}
```

**Important:** The more detail you provide (especially `summary`, `skills`, `workHistory.bullets`), the better the matching quality. These fields get concatenated into a text blob and embedded for vector similarity search.

### 2. Create a job recommendations service in VALET

Build a service that:
1. Takes the user's parsed resume data
2. Builds a `CandidateProfile`
3. Calls `POST {JOBX_BASE_URL}/api/v1/matching/recommendations`
4. Returns the results to the frontend

**Request example:**
```json
{
  "candidate": { ... },
  "top_k": 50,
  "top_n": 10,
  "min_cosine_score": 0.3,
  "enable_llm_rerank": false,
  "excludeJobIds": ["job_id_1", "job_id_2"],
  "preferredCountryCode": "US"
}
```

**Key parameters to expose to the user or configure:**
- `top_n` — how many results to show (default 10)
- `preferredCountryCode` — filter by country
- `min_cosine_score` — lower = more results but less relevant (0.3 is generous, 0.48 is stricter)
- `excludeJobIds` — IDs of jobs already seen/applied/saved

### 3. Create Supabase tables for job tracking

VALET needs to track which jobs a user has interacted with. Create these tables in VALET's Supabase:

```sql
-- Jobs that have been recommended to a user
CREATE TABLE user_job_recommendations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL,           -- JobX job_id (Firestore document ID)
  job_title TEXT NOT NULL,
  job_company TEXT,               -- Extracted from source field (e.g. "anthropic" from "greenhouse:anthropic")
  apply_url TEXT NOT NULL,
  final_score FLOAT NOT NULL,
  cosine_score FLOAT NOT NULL,
  recommended_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, job_id)
);

-- Jobs the user explicitly saved
CREATE TABLE user_saved_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL,           -- JobX job_id
  job_title TEXT NOT NULL,
  job_company TEXT,
  apply_url TEXT NOT NULL,
  saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, job_id)
);

-- Jobs the user applied to (or marked as applied)
CREATE TABLE user_applied_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL,           -- JobX job_id
  job_title TEXT NOT NULL,
  job_company TEXT,
  apply_url TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, job_id)
);

-- Indexes
CREATE INDEX idx_recommendations_user ON user_job_recommendations(user_id);
CREATE INDEX idx_saved_jobs_user ON user_saved_jobs(user_id);
CREATE INDEX idx_applied_jobs_user ON user_applied_jobs(user_id);
```

### 4. Implement exclude_job_ids dedup logic

Before calling JobX, query all three tables to build the exclusion list:

```sql
SELECT DISTINCT job_id FROM (
  SELECT job_id FROM user_job_recommendations WHERE user_id = $1
  UNION
  SELECT job_id FROM user_saved_jobs WHERE user_id = $1
  UNION
  SELECT job_id FROM user_applied_jobs WHERE user_id = $1
) AS seen_jobs;
```

Pass this list as `excludeJobIds` in the matching request so the user never sees the same job twice.

### 5. Build the frontend UI

Recommended pages/components:

#### Job Recommendations Page
- Shows the top N matched jobs with scores
- Each card shows: title, company, location, match score, apply link
- Actions per job: "Save", "Apply" (opens apply_url), "Dismiss"
- "Load More" button that calls the API again with previously seen jobs excluded
- Optional: country filter, score threshold slider

#### Saved Jobs Page
- List of all saved jobs
- Actions: "Remove from saved", "Apply"
- Sort by: saved date, match score

#### Applied Jobs Page
- List of jobs the user marked as applied
- Tracking/status field (optional)

### 6. Handle the "Apply" flow

When user clicks "Apply":
1. Open the `apply_url` in a new tab (this goes directly to the company's application page)
2. Ask the user "Did you apply?" (confirmation dialog)
3. If yes, insert into `user_applied_jobs`
4. The job will be excluded from future recommendations via `excludeJobIds`

### 7. Environment configuration

Add to VALET's environment:

```env
# JobX API
JOBX_BASE_URL=http://localhost:8000   # or deployed URL
JOBX_API_TIMEOUT=30                    # seconds (matching takes ~3s typically)
```

---

## JobX Response Fields Reference

Each result item in the response:

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | Firestore document ID — use this as the stable identifier |
| `source` | string | Format: `platform:identifier` (e.g. `greenhouse:anthropic`) |
| `title` | string | Job title |
| `apply_url` | string | Direct application URL |
| `locations` | array | `[{city, region, country_code, display_name, is_primary, workplace_type}]` |
| `department` | string | Department name |
| `team` | string | Team name |
| `employment_type` | string | e.g. `full_time`, `part_time`, `contract` |
| `cosine_score` | float | Raw vector similarity (0-1, higher = more similar) |
| `skill_overlap_score` | float | How many user skills match the job (0-1) |
| `domain_match_score` | float | 1.0 if same domain, 0.5 if related, 0.0 if different |
| `seniority_match_score` | float | How well seniority levels match (0-1) |
| `final_score` | float | Weighted composite score (this is the main ranking score) |
| `experience_gap` | int | Years of experience gap (negative = overqualified) |
| `penalties` | object | `{experience_penalty, education_penalty, total_penalty}` |
| `score_breakdown` | object | `{cosine_component, skill_component, domain_component, seniority_component}` |
| `llm_adjusted_score` | float | Same as final_score unless LLM rerank is enabled |

**For display purposes**, `final_score` is the primary ranking metric. You could display it as a percentage (e.g. `final_score * 100 = "72% match"`).

---

## Edge Cases to Handle

1. **No resume parsed yet** — Don't call matching. Show a prompt to upload/complete resume first.
2. **Empty skills list** — Matching still works (uses summary + work history for embedding) but quality degrades. Prompt user to add skills.
3. **0 results returned** — Lower `min_cosine_score` (try 0.2) or increase `top_k`. Could also mean no jobs match the user's domain at all.
4. **Job no longer exists** — A saved/applied job might get `status=closed` in JobX. Consider periodically checking saved jobs are still open (call JobX jobs API or just note "this job may no longer be available").
5. **Large excludeJobIds list** — If a user has been recommended hundreds of jobs, the exclusion list grows. This is fine — it's filtered client-side after vector recall. But if `top_k` minus excluded count is very small, increase `top_k` proportionally.
6. **Rate limiting** — JobX's matching endpoint does an external API call to SiliconFlow for embedding. If SiliconFlow is rate-limited, the request will fail with a 503. Implement retry with backoff on VALET's side.

---

## Deployment Notes

- JobX currently runs locally (`uvicorn app.main:app --port 8000`)
- For production, JobX will need to be deployed (Cloud Run or similar)
- The Firestore service account JSON must be available to the deployed instance
- VALET needs network access to JobX's deployed URL
- JobX has no authentication on the matching endpoint currently — add API key auth before deploying publicly

---

## Testing the Integration

You can test the JobX API directly:

```bash
curl -X POST http://localhost:8000/api/v1/matching/recommendations \
  -H "Content-Type: application/json" \
  -d '{
    "candidate": {
      "summary": "Software engineer with 3 years Python experience",
      "skills": ["Python", "AWS", "Docker"],
      "totalYearsExperience": 3,
      "education": [{"degree": "BS", "fieldOfStudy": "Computer Science"}],
      "workHistory": [{"title": "Software Engineer", "company": "Test", "bullets": ["Built APIs"]}]
    },
    "top_k": 50,
    "top_n": 5,
    "min_cosine_score": 0.3
  }'
```

Expected: 200 OK with 5 ranked Anthropic job results in ~3 seconds.
