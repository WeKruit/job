from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models import Job, Location, WorkplaceType
from app.services.domain.location import (
    StructuredLocation,
    build_canonical_key,
    normalize_display_name,
)


def _full_snapshot_geonames_resolver() -> Any:
    # Keep geonames lookup behind package export so tests can monkeypatch
    # app.services.application.full_snapshot_sync.get_geonames_resolver.
    import app.services.application.full_snapshot_sync as full_snapshot_sync

    return full_snapshot_sync.get_geonames_resolver()


def _clean_optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_workplace_type(value: object) -> WorkplaceType:
    if isinstance(value, WorkplaceType):
        return value
    if isinstance(value, str):
        try:
            return WorkplaceType(value)
        except ValueError:
            return WorkplaceType.unknown
    return WorkplaceType.unknown


def _is_structured_location_usable(location: StructuredLocation) -> bool:
    return bool(location.city or location.region or location.country_code)


async def sync_job_location(
    *,
    session=None,
    location_repo=None,
    job_location_repo=None,
    job_id: str,
    structured: StructuredLocation,
    is_primary: bool = False,
    source_raw: str | None = None,
) -> Location:
    """Persist one structured location and link it to a job."""
    # Build repos from session (SQL path) if explicit repos not provided
    if location_repo is None:
        from app.repositories.location import LocationRepository

        location_repo = LocationRepository(session)
    if job_location_repo is None:
        from app.repositories.job_location import JobLocationRepository

        job_location_repo = JobLocationRepository(session)

    canonical_key = build_canonical_key(
        city=structured.city,
        region=structured.region,
        country_code=structured.country_code,
    )

    location_data = Location(
        canonical_key=canonical_key,
        display_name=normalize_display_name(
            city=structured.city,
            region=structured.region,
            country_code=structured.country_code,
        ),
        city=structured.city,
        region=structured.region,
        country_code=structured.country_code,
    )

    location = await location_repo.upsert(location_data)
    await job_location_repo.link(
        job_id=job_id,
        location_id=location.id,
        is_primary=is_primary,
        source_raw=source_raw,
        workplace_type=structured.workplace_type.value,
        remote_scope=structured.remote_scope,
    )
    return location


@dataclass
class StructuredLocationPayload:
    structured: StructuredLocation
    source_raw: str | None = None


def _build_structured_locations(payload: dict[str, Any]) -> list[StructuredLocationPayload]:
    hints = payload.get("location_hints")
    structured_locations: list[StructuredLocationPayload] = []

    if isinstance(hints, list):
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            source_raw = _clean_optional_str(hint.get("source_raw"))
            structured = StructuredLocation(
                city=_clean_optional_str(hint.get("city")),
                region=_clean_optional_str(hint.get("region")),
                country_code=_clean_optional_str(hint.get("country_code")),
                workplace_type=_coerce_workplace_type(hint.get("workplace_type")),
                remote_scope=_clean_optional_str(hint.get("remote_scope")),
            )

            if not structured.country_code and structured.city:
                city_match = _full_snapshot_geonames_resolver().resolve_city(
                    city=structured.city,
                    region=structured.region,
                )
                if city_match:
                    structured.country_code = city_match.country_code
                    structured.region = structured.region or city_match.admin1_code

            if _is_structured_location_usable(structured):
                structured_locations.append(
                    StructuredLocationPayload(structured=structured, source_raw=source_raw)
                )

    if structured_locations:
        return structured_locations

    return []


async def sync_staged_job_locations(
    *,
    session=None,
    location_repo=None,
    job_location_repo=None,
    staged_jobs: list[Job],
    unique_payloads: list[dict[str, Any]],
) -> None:
    payload_by_external_id = {
        str(payload["external_job_id"]): payload for payload in unique_payloads
    }
    for job in staged_jobs:
        payload = payload_by_external_id.get(str(job.external_job_id))
        if not payload:
            continue

        structured_locations = _build_structured_locations(payload)
        for i, location_payload in enumerate(structured_locations):
            is_primary = i == 0
            structured = location_payload.structured
            await sync_job_location(
                session=session,
                location_repo=location_repo,
                job_location_repo=job_location_repo,
                job_id=str(job.id),
                structured=structured,
                is_primary=is_primary,
                source_raw=location_payload.source_raw,
            )
