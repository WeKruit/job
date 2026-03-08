"""Firestore-backed candidate recall using native vector search."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector


async def _load_locations_for_jobs(
    db: AsyncClient, job_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Batch-load location data for multiple jobs. Returns {job_id: [locations]}."""
    if not job_ids:
        return {}

    result: dict[str, list[dict[str, Any]]] = {jid: [] for jid in job_ids}
    location_ids_needed: set[str] = set()
    links_by_job: dict[str, list[dict[str, Any]]] = {jid: [] for jid in job_ids}

    # Firestore 'in' queries support max 30 values per call
    for i in range(0, len(job_ids), 30):
        batch = job_ids[i : i + 30]
        query = db.collection("job_locations").where("job_id", "in", batch)
        async for link_doc in query.stream():
            link = link_doc.to_dict()
            if not link:
                continue
            jid = link.get("job_id")
            if jid:
                links_by_job.setdefault(jid, []).append(link)
                loc_id = link.get("location_id")
                if loc_id:
                    location_ids_needed.add(loc_id)

    # Batch-fetch all unique locations
    loc_cache: dict[str, dict[str, Any]] = {}
    loc_id_list = list(location_ids_needed)
    for i in range(0, len(loc_id_list), 30):
        batch = loc_id_list[i : i + 30]
        refs = [db.collection("locations").document(lid) for lid in batch]
        docs = await asyncio.gather(*(ref.get() for ref in refs))
        for doc in docs:
            if doc.exists:
                loc_cache[doc.id] = doc.to_dict() or {}

    # Assemble locations per job
    for jid in job_ids:
        locations: list[dict[str, Any]] = []
        for link in links_by_job.get(jid, []):
            loc_data: dict[str, Any] = {
                "source_raw": link.get("source_raw"),
                "workplace_type": link.get("workplace_type"),
                "remote_scope": link.get("remote_scope"),
                "is_primary": link.get("is_primary", False),
            }
            loc_id = link.get("location_id")
            if loc_id and loc_id in loc_cache:
                loc = loc_cache[loc_id]
                loc_data["city"] = loc.get("city")
                loc_data["region"] = loc.get("region")
                loc_data["country_code"] = loc.get("country_code")
                loc_data["display_name"] = loc.get("display_name")
            locations.append(loc_data)
        locations.sort(key=lambda x: (not x.get("is_primary", False)))
        result[jid] = locations

    return result


def _extract_jd_experience_years(structured_jd: dict | None) -> int | None:
    if not structured_jd:
        return None
    val = structured_jd.get("experience_years")
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


class FirestoreMatchCandidateGateway:
    """Firestore-native candidate recall using find_nearest vector search."""

    def __init__(self, db: AsyncClient):
        self._db = db

    async def fetch_candidates(
        self,
        *,
        user_embedding: list[float],
        top_k: int,
        exclude_job_ids: list[str] | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """Fetch top_k candidates via Firestore native vector search.

        Returns rows in the same dict format as the SQL MatchCandidateGateway.
        """
        _DISTANCE_FIELD = "vector_distance"
        _MAX_FIND_NEAREST_LIMIT = 1000
        exclude_set = set(exclude_job_ids or [])
        # Over-fetch to compensate for post-query exclusion filtering
        fetch_limit = min(top_k + len(exclude_set), _MAX_FIND_NEAREST_LIMIT)
        query = (
            self._db.collection("jobs")
            .where("status", "==", "open")
            .find_nearest(
                vector_field="embedding",
                query_vector=Vector(user_embedding),
                distance_measure=DistanceMeasure.COSINE,
                limit=fetch_limit,
                distance_result_field=_DISTANCE_FIELD,
            )
        )

        # First pass: collect all candidates from vector search
        candidates: list[tuple[str, dict[str, Any], float]] = []
        source_ids_needed: set[str] = set()

        async for doc in query.stream():
            data = doc.to_dict()
            if not data:
                continue
            job_id = doc.id
            if job_id in exclude_set:
                continue

            distance = data.pop(_DISTANCE_FIELD, None)
            cosine_score = (1.0 - distance) if distance is not None else 0.0

            source_id = data.get("source_id")
            if source_id:
                source_ids_needed.add(source_id)

            candidates.append((job_id, data, cosine_score))

        if not candidates:
            return []

        # Batch-fetch sources
        source_cache: dict[str, str] = {}
        for sid in source_ids_needed:
            source_doc = await self._db.collection("sources").document(sid).get()
            if source_doc.exists:
                s = source_doc.to_dict() or {}
                source_cache[sid] = f"{s.get('platform')}:{s.get('identifier')}"

        # Batch-fetch locations
        job_ids = [jid for jid, _, _ in candidates]
        locations_by_job = await _load_locations_for_jobs(self._db, job_ids)

        # Build rows
        rows: list[dict[str, Any]] = []
        for job_id, data, cosine_score in candidates:
            source_id = data.get("source_id")
            source_key = source_cache.get(source_id) if source_id else None
            locations = locations_by_job.get(job_id, [])
            structured_jd = data.get("structured_jd")

            row: dict[str, Any] = {
                "job_id": job_id,
                "source": source_key,
                "title": data.get("title", ""),
                "apply_url": data.get("apply_url", ""),
                "locations": json.loads(json.dumps(locations, default=str)),
                "department": data.get("department"),
                "team": data.get("team"),
                "employment_type": data.get("employment_type"),
                "sponsorship_not_available": data.get("sponsorship_not_available", "unknown"),
                "job_domain_raw": data.get("job_domain_raw"),
                "job_domain_normalized": data.get("job_domain_normalized", "unknown"),
                "min_degree_level": data.get("min_degree_level", "unknown"),
                "min_degree_rank": data.get("min_degree_rank", -1),
                "structured_jd": structured_jd,
                "jd_experience_years": _extract_jd_experience_years(structured_jd),
                "cosine_score": cosine_score,
            }
            rows.append(row)

        return rows
