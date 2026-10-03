"""``/api/v1`` read routes over the artifact store, one identical set per kind, plus the alert delivery view.

For each kind (``market-intelligence``, ``options-intelligence``, ``trade-setups``, ``invalidation-checks``,
``alerts``):

- ``GET /<path>?symbol=&as_of_from=&as_of_to=&limit=&cursor=``: history, sealed ``as_of`` descending then id
  ascending, ``limit`` 1-200 (default 50), opaque cursor;
- ``GET /<path>/latest?symbol=[&as_of=]``: the greatest sealed ``as_of`` <= ``as_of`` (or overall); a tie is 409;
- ``GET /<path>/{id}``: the summary view;
- ``GET /<path>/{id}/canonical``: the exact stored bytes (``ETag`` = the content id, immutable caching), re-checked
  against the index digest on every read.

``GET /alerts/{id}/deliveries``: the Phase 12E receipts for that alert, in sequence order, from
``MIAS_RECEIPT_ROOT`` via ``alert_engine.receipts.read_receipts`` (never Redis).

Handlers only parse, call the store and project; they hold no analytical logic.
"""
from contextlib import nullcontext
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import Response

from alert_engine.receipts import ReceiptError, read_receipts
from api import views
from api.artifacts import API_VERSION, limit_param, query, served_at, store_errors, store_for
from api.errors import ApiError
from api.projections import DELIVERY_VIEW, PROJECTIONS, VIEW_NAMES, delivery
from api.request_context import current_request_id
from artifact_store.index import query_limit
from artifact_store.layout import ARTIFACT_ID

ROUTES = (("market-intelligence", "market-intelligence", views.MarketIntelligenceResponse,
           views.MarketIntelligenceList),
          ("options-intelligence", "options-intelligence", views.OptionsIntelligenceResponse,
           views.OptionsIntelligenceList),
          ("trade-setups", "trade-setup", views.TradeSetupResponse, views.TradeSetupList),
          ("invalidation-checks", "invalidation-check", views.InvalidationCheckResponse,
           views.InvalidationCheckList),
          ("alerts", "alert", views.AlertResponse, views.AlertList))
CANONICAL_CACHE = "private, max-age=31536000, immutable"


def _meta(request, view, **extra):
    return dict(api_version=API_VERSION, view=view, request_id=current_request_id(), served_at=served_at(request),
                **extra)


def _checked_id(artifact_id):
    if not ARTIFACT_ID.fullmatch(artifact_id):
        raise ApiError("invalid_request")
    return artifact_id


def _span(request, kind, operation):
    """The Phase 15 artifact-lookup span (kind, operation, closed result); a no-op without telemetry."""
    telemetry = getattr(request.app.state, "telemetry", None)
    return telemetry.artifact_span(kind, operation) if telemetry is not None else nullcontext()


def _view(store, kind, entry):
    return PROJECTIONS[kind](json.loads(store.read_bytes(entry)))


def _register(router, path, kind, item_model, list_model):
    view_name = VIEW_NAMES[kind]

    def history(request: Request):
        params = query(request, ("symbol", "as_of_from", "as_of_to", "limit", "cursor"))
        store = store_for(request)
        with _span(request, kind, "history"), store_errors():
            limit = query_limit(limit_param(params.get("limit")))
            entries, cursor = store.snapshot().history(
                kind, symbol=params.get("symbol"), as_of_from=params.get("as_of_from"),
                as_of_to=params.get("as_of_to"), limit=limit, cursor=params.get("cursor"))
            data = [_view(store, kind, e) for e in entries]
        return {"data": data, "meta": _meta(request, view_name, limit=limit, next_cursor=cursor)}

    def latest(request: Request):
        params = query(request, ("symbol", "as_of"), required=("symbol",))
        store = store_for(request)
        with _span(request, kind, "latest"), store_errors():
            entry = store.snapshot().latest(kind, params["symbol"], params.get("as_of"))
            return {"data": _view(store, kind, entry), "meta": _meta(request, view_name)}

    def get(request: Request, artifact_id: str):
        query(request, ())
        store = store_for(request)
        with _span(request, kind, "get"), store_errors():
            entry = store.snapshot().get(kind, _checked_id(artifact_id))
            return {"data": _view(store, kind, entry), "meta": _meta(request, view_name)}

    def canonical(request: Request, artifact_id: str):
        query(request, ())
        store = store_for(request)
        with _span(request, kind, "canonical"), store_errors():
            entry = store.snapshot().get(kind, _checked_id(artifact_id))
            raw = store.read_bytes(entry)
        return Response(content=raw, media_type="application/json",
                        headers={"ETag": f'"{entry.artifact_id}"', "Cache-Control": CANONICAL_CACHE})

    name = kind.replace("-", "_")
    router.add_api_route(f"/{path}", history, methods=["GET"], response_model=list_model, name=f"{name}_history")
    router.add_api_route(f"/{path}/latest", latest, methods=["GET"], response_model=item_model, name=f"{name}_latest")
    router.add_api_route(f"/{path}/{{artifact_id}}", get, methods=["GET"], response_model=item_model,
                         name=f"{name}_get")
    router.add_api_route(f"/{path}/{{artifact_id}}/canonical", canonical, methods=["GET"], name=f"{name}_canonical",
                         response_class=Response,
                         responses={200: {"content": {"application/json": {}},
                                          "description": "The exact stored canonical bytes."}})
    if kind == "alert":
        def deliveries(request: Request, artifact_id: str):
            query(request, ())
            store = store_for(request)
            with store_errors():
                store.snapshot().get(kind, _checked_id(artifact_id))
            root = request.app.state.settings.receipt_root
            if root is None or not os.path.isdir(root):
                raise ApiError("dependency_unavailable")
            try:
                history = read_receipts(root)
            except ReceiptError:
                raise ApiError("artifact_invalid") from None
            data = [delivery(r) for r in history if r["alert_id"] == artifact_id]
            return {"data": data, "meta": _meta(request, DELIVERY_VIEW)}
        router.add_api_route(f"/{path}/{{artifact_id}}/deliveries", deliveries, methods=["GET"],
                             response_model=views.DeliveryList, name="alert_deliveries")


def build_router():
    router = APIRouter(prefix="/api/v1", tags=["artifacts"])
    for path, kind, item_model, list_model in ROUTES:
        _register(router, path, kind, item_model, list_model)
    return router
