"""API response views (``api-v1``). These describe HTTP responses only; canonical analytical objects are never
modelled or re-serialized here."""
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorDetail(_View):
    code: str
    message: str
    request_id: str


class ErrorView(_View):
    error: ErrorDetail


class LivenessView(_View):
    status: Literal["live"]


class CheckView(_View):
    name: str
    status: Literal["pass", "fail"]


class ReadinessView(_View):
    status: Literal["ready", "not_ready"]
    checks: list[CheckView]


class AnalyticalFormat(_View):
    object: str
    format_version: str
    rules_version: str


class VersionView(_View):
    service: str
    api_version: str
    build: str
    analytical_formats: list[AnalyticalFormat]
