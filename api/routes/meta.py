"""``GET /api/v1/version``: service metadata and the analytical format versions MIAS reads, under the read policy.

Only side-effect-free rule modules are imported (constants; standard library dependencies only). No clock.
"""
from fastapi import APIRouter, Request

from alert_engine import rules as alert_rules
from api.views import VersionView
from market_intelligence import rules as mi_rules
from options_intelligence import rules as oi_rules
from trade_setup import rules as setup_rules

SERVICE = "mias-api"
API_VERSION = "v1"
ANALYTICAL_FORMATS = tuple(
    [dict(object="market_intelligence", format_version=mi_rules.INTELLIGENCE_FORMAT_VERSION,
          rules_version=mi_rules.RULES_VERSION)]
    + [dict(object="options_intelligence", format_version=fmt, rules_version=rules)
       for fmt, rules in sorted(oi_rules.FORMATS.items())]
    + [dict(object="trade_setup_assessment", format_version=setup_rules.ASSESSMENT_FORMAT_VERSION,
            rules_version=setup_rules.RULES_VERSION),
       dict(object="invalidation_check", format_version=setup_rules.INVALIDATION_FORMAT_VERSION,
            rules_version=setup_rules.INVALIDATION_RULES_VERSION),
       dict(object="alert_event", format_version=alert_rules.ALERT_FORMAT_VERSION,
            rules_version=alert_rules.RULES_VERSION)])

router = APIRouter(prefix="/api/v1", tags=["meta"])


@router.get("/version", response_model=VersionView)
def version(request: Request):
    return {"service": SERVICE, "api_version": API_VERSION, "build": request.app.state.settings.build_id,
            "analytical_formats": list(ANALYTICAL_FORMATS)}
