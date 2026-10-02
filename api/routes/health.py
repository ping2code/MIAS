"""Unversioned, unauthenticated health routes.

- ``GET /health/live``: 200 whenever the process can answer. It touches no dependency, file or check.
- ``GET /health/ready``: 200 when every readiness check passes, else 503. The body lists only check names and
  ``pass``/``fail``.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.readiness import FAIL, run_checks
from api.views import LivenessView, ReadinessView

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=LivenessView)
def live():
    return {"status": "live"}


@router.get("/health/ready", response_model=ReadinessView, responses={503: {"model": ReadinessView}})
def ready(request: Request):
    results = run_checks(request.app.state.readiness_checks)
    ready_ = all(status != FAIL for _, status in results)
    body = {"status": "ready" if ready_ else "not_ready",
            "checks": [{"name": name, "status": status} for name, status in results]}
    return JSONResponse(body, status_code=200 if ready_ else 503)
