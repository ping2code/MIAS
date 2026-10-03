"""The application factory: ``create_app(settings, *, readiness_checks=None, artifact_store=None, clock=None,
refresh_interval=REFRESH_INTERVAL_SECONDS)``.

It builds a FastAPI app from validated ``ApiSettings``: no environment, network, Redis or database access, and no
server start. The artifact store (from ``MIAS_ARTIFACT_ROOT``, or injected) does no I/O until the app's lifespan
starts: then it builds the index once and refreshes it every ``refresh_interval`` seconds in a background thread
(``0`` disables the thread), stopping at shutdown. Routes are registered in a fixed order:

- health (open): ``/health/live``, ``/health/ready``;
- ``/api/v1`` (read policy): ``/api/v1/version``, then the artifact read routes;
- ``/docs`` and ``/openapi.json`` only when ``docs_enabled`` (ReDoc is off).

Debug is off and there's no CORS middleware. ``readiness_checks`` replaces the default checks. ``clock`` supplies
the runtime ``served_at`` of views (API metadata only). ``telemetry`` (Phase 15, ``api.observability.Telemetry``)
defaults to a disabled instance: OpenTelemetry API no-ops, nothing exported.
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from api.artifacts import REFRESH_INTERVAL_SECONDS, Refresher, utc_now
from api.auth import require_read
from api.errors import install_error_handlers
from api.observability import Telemetry
from api.readiness import ReadinessCheck, default_checks
from api.request_context import RequestContextMiddleware
from api.routes import artifacts, health, meta
from api.settings import ApiSettings
from api.views import ErrorView
from artifact_store.service import ArtifactStore

TITLE = "MIAS API"


def create_app(settings, *, readiness_checks=None, artifact_store=None, clock=None,
               refresh_interval=REFRESH_INTERVAL_SECONDS, telemetry=None):
    if not isinstance(settings, ApiSettings):
        raise TypeError("create_app needs validated ApiSettings")
    if artifact_store is None and settings.artifact_root is not None:
        artifact_store = ArtifactStore(settings.artifact_root)
    if artifact_store is not None and not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact_store must be an ArtifactStore")
    checks = default_checks(settings, artifact_store) if readiness_checks is None else tuple(readiness_checks)
    if not all(isinstance(c, ReadinessCheck) for c in checks) or len({c.name for c in checks}) != len(checks):
        raise ValueError("readiness checks must be uniquely named ReadinessCheck objects")

    telemetry = telemetry if telemetry is not None else Telemetry(service_version=settings.build_id)

    @asynccontextmanager
    async def lifespan(app):
        refresher = Refresher(artifact_store, refresh_interval, telemetry) if artifact_store is not None else None
        telemetry.start(artifact_store, refresher)
        if refresher is not None:
            refresher.start()
        try:
            yield
        finally:
            if refresher is not None:
                refresher.stop()
            telemetry.shutdown()

    # FastAPI's native telemetry is off: it would read OTEL_* itself, install *global* SDK providers and instrument
    # requests with attributes MIAS does not control. MIAS instruments explicitly (api.observability).
    app = FastAPI(title=TITLE, version=meta.API_VERSION, debug=False, lifespan=lifespan,
                  telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                             "auto_configure": False},
                  docs_url="/docs" if settings.docs_enabled else None, redoc_url=None,
                  openapi_url="/openapi.json" if settings.docs_enabled else None,
                  responses={"default": {"model": ErrorView}})
    app.state.settings = settings
    app.state.readiness_checks = checks
    app.state.artifact_store = artifact_store
    app.state.clock = clock or utc_now
    app.state.telemetry = telemetry
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(meta.router, dependencies=[Depends(require_read)])
    app.include_router(artifacts.build_router(), dependencies=[Depends(require_read)])
    app.add_middleware(RequestContextMiddleware)
    return app
