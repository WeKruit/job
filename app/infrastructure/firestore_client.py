"""Singleton Firestore client initialised from app settings."""

from __future__ import annotations

import json
import logging
from functools import lru_cache

import firebase_admin
from firebase_admin import credentials
from google.cloud.firestore_v1.async_client import AsyncClient
from google.oauth2 import service_account as sa

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_app: firebase_admin.App | None = None


def _init_firebase_app() -> firebase_admin.App:
    """Initialise the Firebase Admin SDK (idempotent)."""
    global _app
    if _app is not None:
        return _app

    settings = get_settings()

    if settings.firestore_credentials_file:
        cred = credentials.Certificate(settings.firestore_credentials_file)
        kwargs: dict = {"credential": cred}
        if settings.firestore_project_id:
            kwargs["options"] = {"projectId": settings.firestore_project_id}
        _app = firebase_admin.initialize_app(**kwargs)
        logger.info(
            "Firebase initialised from credentials file: %s",
            settings.firestore_credentials_file,
        )
    else:
        _app = firebase_admin.initialize_app()
        logger.info("Firebase initialised with Application Default Credentials")

    return _app


@lru_cache
def get_firestore_client() -> AsyncClient:
    """Return a cached async Firestore client."""
    _init_firebase_app()
    settings = get_settings()

    if settings.firestore_credentials_file:
        with open(settings.firestore_credentials_file) as f:
            info = json.load(f)
        gcp_creds = sa.Credentials.from_service_account_info(info)
        client = AsyncClient(project=info["project_id"], credentials=gcp_creds)
    else:
        client = AsyncClient()

    logger.info("Firestore async client ready (project=%s)", client.project)
    return client
