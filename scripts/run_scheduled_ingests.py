#!/usr/bin/env python3
"""Run scheduled ingests with SyncRun tracking and source-level retries."""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.models import PlatformType, Source, SyncRun, SyncRunStatus, build_source_key
from app.services.application.sync import SUPPORTED_PLATFORMS, SyncService


async def _load_candidate_sources_firestore(
    *,
    platform: PlatformType | None,
    identifier: str | None,
    limit: int | None,
) -> tuple[list[Source], list[Source]]:
    from app.infrastructure.firestore_client import get_firestore_client
    from app.repositories.firestore import FirestoreSourceRepository

    db = get_firestore_client()
    repo = FirestoreSourceRepository(db)
    all_sources = await repo.list(enabled=True, platform=platform)

    if identifier is not None:
        all_sources = [s for s in all_sources if s.identifier == identifier]

    supported_sources = [s for s in all_sources if s.platform in SUPPORTED_PLATFORMS]
    unsupported_sources = [s for s in all_sources if s.platform not in SUPPORTED_PLATFORMS]
    if limit is not None:
        supported_sources = supported_sources[:limit]
    return supported_sources, unsupported_sources


async def _load_candidate_sources_sql(
    *,
    platform: PlatformType | None,
    identifier: str | None,
    limit: int | None,
) -> tuple[list[Source], list[Source]]:
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.core.database import engine

    async with AsyncSession(engine) as session:
        statement = (
            select(Source)
            .where(Source.enabled.is_(True))
            .order_by(Source.platform, Source.identifier)
        )
        if platform is not None:
            statement = statement.where(Source.platform == platform)
        if identifier is not None:
            statement = statement.where(Source.identifier == identifier)

        rows = await session.exec(statement)
        all_sources = list(rows.all())

    supported_sources = [source for source in all_sources if source.platform in SUPPORTED_PLATFORMS]
    unsupported_sources = [
        source for source in all_sources if source.platform not in SUPPORTED_PLATFORMS
    ]
    if limit is not None:
        supported_sources = supported_sources[:limit]
    return supported_sources, unsupported_sources


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    use_firestore = bool(settings.firestore_credentials_file)

    if not use_firestore:
        from sqlmodel import SQLModel

        from app.core.database import engine

        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    platform = PlatformType(args.platform) if args.platform is not None else None
    if platform is not None and platform not in SUPPORTED_PLATFORMS:
        print(f"unsupported_platform={platform.value}")
        return 1

    # Enforce ingest_max_sources cap (overridable via --limit but capped by config)
    effective_limit = args.limit
    max_sources = settings.ingest_max_sources
    if effective_limit is None or effective_limit > max_sources:
        effective_limit = max_sources
        print(f"safety_cap: limiting to {max_sources} sources (INGEST_MAX_SOURCES={max_sources})")

    if use_firestore:
        sources, unsupported_sources = await _load_candidate_sources_firestore(
            platform=platform,
            identifier=args.identifier,
            limit=effective_limit,
        )
    else:
        sources, unsupported_sources = await _load_candidate_sources_sql(
            platform=platform,
            identifier=args.identifier,
            limit=effective_limit,
        )

    if unsupported_sources:
        print(
            "warning_unsupported_sources="
            + ",".join(
                build_source_key(source.platform, source.identifier)
                for source in unsupported_sources
            )
        )
        if args.identifier is not None and not sources:
            return 1

    print(f"backend={'firestore' if use_firestore else 'postgres'}")
    print(f"target_sources={len(sources)}")
    print(f"include_content={args.include_content}")
    print(f"dry_run={args.dry_run}")
    print(f"retry_attempts={args.retry_attempts}")

    if not args.dry_run and sources and not args.yes:
        source_names = ", ".join(
            build_source_key(s.platform, s.identifier) for s in sources
        )
        answer = input(
            f"\n⚠ About to WRITE to the database for {len(sources)} source(s): {source_names}\n"
            f"  This will insert/update/close jobs. Continue? [y/N] "
        )
        if answer.strip().lower() != "y":
            print("Aborted.")
            return 1

    engine_arg = None
    if not use_firestore:
        from app.core.database import engine

        engine_arg = engine
    sync_service = SyncService(engine=engine_arg)
    failures: list[tuple[Source, SyncRun]] = []
    totals = {
        "fetched": 0,
        "mapped": 0,
        "unique": 0,
        "deduped_by_external_id": 0,
        "inserted": 0,
        "updated": 0,
        "closed": 0,
    }

    for idx, source in enumerate(sources, start=1):
        sync_run = await sync_service.sync_source(
            source=source,
            include_content=args.include_content,
            dry_run=args.dry_run,
            retry_attempts=args.retry_attempts,
        )
        source_key = build_source_key(source.platform, source.identifier)
        prefix = f"[{idx}/{len(sources)}] {source.identifier} ({source_key})"

        totals["fetched"] += sync_run.fetched_count
        totals["mapped"] += sync_run.mapped_count
        totals["unique"] += sync_run.unique_count
        totals["deduped_by_external_id"] += sync_run.deduped_by_external_id
        totals["inserted"] += sync_run.inserted_count
        totals["updated"] += sync_run.updated_count
        totals["closed"] += sync_run.closed_count

        if sync_run.status == SyncRunStatus.success:
            print(
                f"{prefix}: sync_run_id={sync_run.id}, status={sync_run.status.value}, "
                f"fetched={sync_run.fetched_count}, unique={sync_run.unique_count}, "
                f"deduped_by_external_id={sync_run.deduped_by_external_id}, "
                f"inserted={sync_run.inserted_count}, updated={sync_run.updated_count}, "
                f"closed={sync_run.closed_count}"
            )
        else:
            failures.append((source, sync_run))
            print(
                f"{prefix}: sync_run_id={sync_run.id}, status={sync_run.status.value}, "
                f"error={sync_run.error_summary}"
            )

    print("=== SUMMARY ===")
    print(f"sources_total={len(sources)}")
    print(f"sources_success={len(sources) - len(failures)}")
    print(f"sources_failed={len(failures)}")
    print(f"jobs_fetched_total={totals['fetched']}")
    print(f"jobs_mapped_total={totals['mapped']}")
    print(f"jobs_unique_total={totals['unique']}")
    print(f"jobs_deduped_by_external_id_total={totals['deduped_by_external_id']}")
    print(f"jobs_inserted_total={totals['inserted']}")
    print(f"jobs_updated_total={totals['updated']}")
    print(f"jobs_closed_total={totals['closed']}")
    if failures:
        print("failed_sources=" + ",".join(source.identifier for source, _ in failures))
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run scheduled ingest syncs for enabled sources.")
    parser.add_argument(
        "--platform",
        choices=[platform.value for platform in PlatformType],
        default=None,
        help="Filter to one platform. Unsupported but valid platforms exit non-zero.",
    )
    parser.add_argument(
        "--identifier",
        default=None,
        help="Exact Source.identifier to run. Defaults to all enabled supported sources.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit number of target sources.")
    parser.add_argument(
        "--include-content",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include full job content for platforms that support detail fetches.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run syncs but rollback job writes.")
    parser.add_argument(
        "--retry-attempts", type=int, default=3, help="Retry attempts per source sync."
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt."
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
