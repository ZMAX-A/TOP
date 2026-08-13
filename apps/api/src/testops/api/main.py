"""FastAPI control-plane entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from secrets import compare_digest

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from testops.contracts import capability_payload
from testops.contracts.schema_export import schemas

from .artifact_store import MinioArtifactStore
from .config import Settings
from .database import create_database_runtime, create_schema, database_ready
from .governance_routes import router as governance_router
from .identity_routes import router as identity_router
from .observability import ApiMetrics, PrometheusMiddleware
from .routes import router
from .services import ServiceError

API_VERSION = "0.10.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    engine, session_factory = create_database_runtime(resolved_settings)
    metrics = ApiMetrics(API_VERSION)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if resolved_settings.auto_create_schema:
            await create_schema(engine)
        yield
        await engine.dispose()

    application = FastAPI(
        title="TestOps Platform API",
        version=API_VERSION,
        description="Control plane for governed, reproducible automated test execution.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.artifact_store = MinioArtifactStore.from_settings(resolved_settings)
    application.state.metrics = metrics
    application.add_middleware(PrometheusMiddleware, metrics=metrics)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
            "X-Bootstrap-Token",
        ],
    )

    @application.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @application.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "testops-api", "version": application.version}

    @application.get("/readyz", tags=["system"])
    async def readyz() -> JSONResponse:
        ready = await database_ready(engine)
        metrics.database_ready.set(1 if ready else 0)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "database": ready},
        )

    @application.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        expected_token = resolved_settings.metrics_token
        if expected_token is not None:
            scheme, separator, candidate = request.headers.get("Authorization", "").partition(" ")
            authorized = (
                separator == " "
                and scheme.lower() == "bearer"
                and compare_digest(candidate, expected_token)
            )
            if not authorized:
                return Response(
                    content="metrics authentication required\n",
                    status_code=401,
                    media_type="text/plain",
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
        metrics.database_ready.set(1 if await database_ready(engine) else 0)
        return Response(
            content=metrics.render(),
            headers={
                "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
                "Cache-Control": "no-store",
            },
        )

    @application.get("/api/v1/contracts/capabilities", tags=["contracts"])
    def capabilities() -> dict[str, list[dict[str, object]]]:
        return capability_payload()

    @application.get("/api/v1/contracts/schemas/{schema_name}", tags=["contracts"])
    def contract_schema(schema_name: str) -> dict[str, object]:
        filename = f"{schema_name}.schema.json"
        try:
            return schemas()[filename]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown contract schema") from exc

    application.include_router(identity_router)
    application.include_router(governance_router)
    application.include_router(router)
    return application


app = create_app()
