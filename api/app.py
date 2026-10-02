"""The application factory: ``create_app(settings, *, readiness_checks=None)``.

It builds a FastAPI app from validated ``ApiSettings`` and nothing else: no environment, file, network, Redis or
database access, and no server start. Routes are registered in a fixed order:

- health (open): ``/health/live``, ``/health/ready``;
- ``/api/v1`` (read policy): ``/api/v1/version``;
- ``/docs`` and ``/openapi.json`` only when ``docs_enabled`` (ReDoc is off).

Debug is off and there's no CORS middleware. ``readiness_checks`` replaces the default checks (used by tests and,
later, by the artifact store).
"""
from fastapi import Depends, FastAPI

from api.auth import require_read
from api.errors import install_error_handlers
from api.readiness import ReadinessCheck, default_checks
from api.request_context import RequestContextMiddleware
from api.routes import health, meta
from api.settings import ApiSettings
from api.views import ErrorView

TITLE = "MIAS API"


def create_app(settings, *, readiness_checks=None):
    if not isinstance(settings, ApiSettings):
        raise TypeError("create_app needs validated ApiSettings")
    checks = default_checks(settings) if readiness_checks is None else tuple(readiness_checks)
    if not all(isinstance(c, ReadinessCheck) for c in checks) or len({c.name for c in checks}) != len(checks):
        raise ValueError("readiness checks must be uniquely named ReadinessCheck objects")
    app = FastAPI(title=TITLE, version=meta.API_VERSION, debug=False,
                  docs_url="/docs" if settings.docs_enabled else None, redoc_url=None,
                  openapi_url="/openapi.json" if settings.docs_enabled else None,
                  responses={"default": {"model": ErrorView}})
    app.state.settings = settings
    app.state.readiness_checks = checks
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(meta.router, dependencies=[Depends(require_read)])
    app.add_middleware(RequestContextMiddleware)
    return app
