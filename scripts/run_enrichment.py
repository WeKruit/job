#!/usr/bin/env python3
"""Enrich Firestore jobs: parse structured JD via LLM + generate embeddings."""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    use_firestore = bool(settings.firestore_credentials_file)
    if not use_firestore:
        print("ERROR: FIRESTORE_CREDENTIALS_FILE not set. This script is Firestore-only.")
        return 1

    from app.infrastructure.firestore_client import get_firestore_client
    from app.repositories.firestore import (
        FirestoreJobEmbeddingRepository,
        FirestoreJobRepository,
    )
    from app.services.application.blob.job_blob import JobBlobManager
    from app.services.application.jd_parsing.jd_service import JDService
    from app.services.application.embedding_refresh.service import EmbeddingRefreshService
    from app.services.infra.blob_storage import NoOpBlobStorage

    db = get_firestore_client()
    job_repo = FirestoreJobRepository(db)
    embedding_repo = FirestoreJobEmbeddingRepository(db)
    blob_manager = JobBlobManager(storage=NoOpBlobStorage())

    # --- Phase A: JD Parsing ---
    if not args.skip_jd:
        print("=== JD PARSING ===")
        jd_service = JDService(repository=job_repo, blob_manager=blob_manager)

        total_parsed = 0
        batch_num = 0
        while True:
            pending = await jd_service.fetch_pending_jobs(
                limit=args.jd_batch_size,
                version_only=args.version_only,
            )
            if not pending:
                break
            batch_num += 1
            print(f"  batch {batch_num}: parsing {len(pending)} jobs...")
            try:
                result = await jd_service.parse_jobs(pending, persist=True)
                total_parsed += len(result.jobs)
                print(f"  batch {batch_num}: parsed {len(result.jobs)} jobs")
            except Exception as exc:
                print(f"  batch {batch_num}: FAILED - {exc}")
                if not args.continue_on_error:
                    return 1
            if args.jd_limit and total_parsed >= args.jd_limit:
                break
        print(f"jd_total_parsed={total_parsed}")
    else:
        print("=== JD PARSING SKIPPED ===")

    # --- Phase B: Embedding Generation ---
    if not args.skip_embeddings:
        print("=== EMBEDDING GENERATION ===")

        # Get all source IDs to iterate over
        from app.repositories.firestore import FirestoreSourceRepository

        source_repo = FirestoreSourceRepository(db)
        all_sources = await source_repo.list(enabled=True)
        source_ids = [str(s.id) for s in all_sources]
        print(f"  sources to process: {len(source_ids)}")

        total_refreshed = 0
        for source_id in source_ids:
            refresh_service = EmbeddingRefreshService(
                session=None,
                job_repository=job_repo,
                job_embedding_repository=embedding_repo,
            )
            result = await refresh_service.refresh_for_source(
                source_id=source_id,
            )
            if result.triggered:
                print(
                    f"  source={source_id}: selected={result.selected_jobs}, "
                    f"refreshed={result.refreshed_jobs}, failed={result.failed_jobs}"
                    + (f", error={result.error}" if result.error else "")
                )
                total_refreshed += result.refreshed_jobs
        print(f"embedding_total_refreshed={total_refreshed}")
    else:
        print("=== EMBEDDING GENERATION SKIPPED ===")

    print("=== DONE ===")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich Firestore jobs (JD parsing + embeddings).")
    parser.add_argument(
        "--jd-batch-size", type=int, default=10,
        help="Jobs per LLM batch for JD parsing (default: 10)",
    )
    parser.add_argument(
        "--jd-limit", type=int, default=None,
        help="Max total jobs to parse JDs for (default: all pending)",
    )
    parser.add_argument(
        "--version-only", action="store_true",
        help="Only re-parse jobs with outdated structured_jd_version",
    )
    parser.add_argument(
        "--skip-jd", action="store_true",
        help="Skip JD parsing, only generate embeddings",
    )
    parser.add_argument(
        "--skip-embeddings", action="store_true",
        help="Skip embedding generation, only parse JDs",
    )
    parser.add_argument(
        "--continue-on-error", action="store_true",
        help="Continue processing after a batch failure",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
