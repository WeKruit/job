import logging
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.monitoring import get_metrics_snapshot, http_metrics

configure_logging()
logger = logging.getLogger("app.http")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    if settings.firestore_credentials_file:
        from app.infrastructure.firestore_client import get_firestore_client

        client = get_firestore_client()
        logger.info("Firestore ready (project=%s)", client.project)
    else:
        from app.core.database import init_db

        await init_db()
        logger.info("SQL database initialized")
    yield
    # Shutdown


app = FastAPI(
    title=settings.app_name,
    description="Job aggregation microservice",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api/v1")


def _resolve_route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return request.url.path


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    http_metrics.request_started()
    started_at = perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (perf_counter() - started_at) * 1000
        route_label = _resolve_route_label(request)
        http_metrics.request_finished(
            method=request.method,
            route_label=route_label,
            status_code=500,
            duration_ms=duration_ms,
        )
        logger.exception(
            "request_failed method=%s path=%s route=%s status_code=%s duration_ms=%.3f request_id=%s",
            request.method,
            request.url.path,
            route_label,
            500,
            duration_ms,
            request_id,
        )
        raise

    duration_ms = (perf_counter() - started_at) * 1000
    route_label = _resolve_route_label(request)
    http_metrics.request_finished(
        method=request.method,
        route_label=route_label,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request method=%s path=%s route=%s status_code=%s duration_ms=%.3f request_id=%s",
        request.method,
        request.url.path,
        route_label,
        response.status_code,
        duration_ms,
        request_id,
    )
    return response


@app.middleware("http")
async def enforce_read_only(request: Request, call_next):
    if settings.read_only_mode and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        # Allow non-API endpoints (health, metrics)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        # Allow read-only POST endpoints (matching is a query, not a mutation)
        if request.url.path.startswith("/api/v1/matching/"):
            return await call_next(request)
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=403,
            content={
                "detail": "READ_ONLY_MODE is enabled. Set READ_ONLY_MODE=false in .env to allow writes."
            },
        )
    return await call_next(request)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "read_only_mode": str(settings.read_only_mode).lower(),
        "backend": "firestore" if settings.firestore_credentials_file else "postgres",
    }


@app.get("/metrics")
async def metrics() -> dict[str, object]:
    return get_metrics_snapshot()
