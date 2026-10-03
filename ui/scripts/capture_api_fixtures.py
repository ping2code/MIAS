"""Regenerate the UI test fixtures from the real mias-api (Phase 16C).

Runs ``create_app`` with FastAPI's TestClient over the sealed Phase 13 test samples (``tests.test_artifact_store
.samples``), published through the artifact-store CLI into temporary directories. Nothing is invented: every body,
status and relevant header is what the API returned. The read token is a placeholder; no network, Redis, PostgreSQL
or provider is touched.

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python ui/scripts/capture_api_fixtures.py > ui/src/tests/fixtures/phase16c.json
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from alert_engine import receipts as rc  # noqa: E402
from alert_engine.delivery.base import DELIVERED, FAILED, DeliveryResult  # noqa: E402
from alert_engine.rendering import delivery_request  # noqa: E402
from api.app import create_app  # noqa: E402
from api.settings import load_api_settings  # noqa: E402
from artifact_store import runner  # noqa: E402
from tests import trade_setup_cases as ts  # noqa: E402
from tests.test_artifact_store import ID_FIELD, samples  # noqa: E402

TOKEN = "placeholder-read-token-0123456789abcdef"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "ui-000000000000000000000001"}
KEEP = ("etag", "cache-control", "content-type", "x-request-id")


def publish(root, src, kind, obj):
    path = os.path.join(src, f"{kind}-{obj[ID_FIELD[kind]][7:]}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2)
    err = io.StringIO()
    code = runner.main(["publish", "--kind", kind, "--file", path], out=io.StringIO(), err=err,
                       environ={"MIAS_ARTIFACT_ROOT": root})
    assert code == 0, err.getvalue()


def capture(client, out, name, path, text=False):
    response = client.get(path, headers=HEADERS)
    entry = {"request": path, "status": response.status_code,
             "headers": {k: v for k, v in response.headers.items() if k.lower() in KEEP}}
    if text:
        entry["text"] = response.text
    else:
        entry["body"] = response.json()
    out[name] = entry
    return entry


def app_for(tmp, objects, receipts=None):
    root, src = os.path.join(tmp, "artifacts"), os.path.join(tmp, "src")
    os.mkdir(root)
    os.mkdir(src)
    for kind, items in objects.items():
        for obj in items:
            publish(root, src, kind, obj)
    environ = {"MIAS_ARTIFACT_ROOT": root, "MIAS_API_READ_TOKEN": TOKEN, "MIAS_BUILD_ID": "phase13e-test"}
    if receipts is not None:
        environ["MIAS_RECEIPT_ROOT"] = receipts
    return create_app(load_api_settings(environ), refresh_interval=0)


def main():
    out = {}
    objects = samples()
    mis = [o["intelligence_id"] for o in objects["market-intelligence"]]
    alerts = [o["alert_id"] for o in objects["alert"]]
    with tempfile.TemporaryDirectory() as tmp:
        with TestClient(app_for(tmp, objects)) as client:          # the deployed shape: no receipt root
            for family, ids in (("market-intelligence", mis), ("alerts", alerts)):
                capture(client, out, f"{family}:history", f"/api/v1/{family}")
                first = capture(client, out, f"{family}:page1", f"/api/v1/{family}?limit=2")
                cursor = first["body"]["meta"]["next_cursor"]
                capture(client, out, f"{family}:page2", f"/api/v1/{family}?limit=2&cursor={cursor}")
                capture(client, out, f"{family}:symbol", f"/api/v1/{family}?symbol=META")
                capture(client, out, f"{family}:window",
                        f"/api/v1/{family}?as_of_from=2026-09-24T00:00:00Z&as_of_to=2026-09-25T23:59:59Z")
                capture(client, out, f"{family}:empty", f"/api/v1/{family}?symbol=ZZZZ")
                capture(client, out, f"{family}:latest", f"/api/v1/{family}/latest?symbol=META")
                capture(client, out, f"{family}:latest_none", f"/api/v1/{family}/latest?symbol=ZZZZ")
                capture(client, out, f"{family}:not_found", f"/api/v1/{family}/sha256:{'0' * 64}")
                capture(client, out, f"{family}:bad_cursor", f"/api/v1/{family}?cursor=bogus")
                capture(client, out, f"{family}:bad_instant", f"/api/v1/{family}?as_of_from=2026-09-24T00:00:00")
                for index, artifact_id in enumerate(ids):
                    capture(client, out, f"{family}:detail:{index}", f"/api/v1/{family}/{artifact_id}")
                    capture(client, out, f"{family}:canonical:{index}", f"/api/v1/{family}/{artifact_id}/canonical",
                            text=True)
            capture(client, out, "alerts:deliveries_unavailable", f"/api/v1/alerts/{alerts[0]}/deliveries")
            for family in ("options-intelligence", "trade-setups", "invalidation-checks"):   # empty history pages
                capture(client, out, f"{family}:empty", f"/api/v1/{family}?symbol=ZZZZ")
    with tempfile.TemporaryDirectory() as tmp:                         # receipts configured (not the lab deployment)
        receipts = os.path.join(tmp, "receipts")
        os.mkdir(receipts)
        when = "2026-10-02T13:30:00.000000+00:00"
        first = objects["alert"][0]
        for result in (DeliveryResult(FAILED, None, 3, "timeout"), DeliveryResult(DELIVERED, "777", 1, None)):
            rc.append_receipt(receipts, delivery_request(first, "telegram"), result, when, when)
        with TestClient(app_for(tmp, objects, receipts)) as client:
            capture(client, out, "alerts:deliveries", f"/api/v1/alerts/{alerts[0]}/deliveries")
            capture(client, out, "alerts:deliveries_empty", f"/api/v1/alerts/{alerts[1]}/deliveries")
    with tempfile.TemporaryDirectory() as tmp:                         # two sealed MIs share symbol and as_of: a tie
        tie = {"market-intelligence": [ts.market_intelligence("all_bullish"), ts.market_intelligence("all_bearish")]}
        with TestClient(app_for(tmp, tie)) as client:
            capture(client, out, "market-intelligence:latest_ambiguous", "/api/v1/market-intelligence/latest?symbol=META")
    with tempfile.TemporaryDirectory() as tmp:                         # health endpoints: ready, and a real 503
        with TestClient(app_for(tmp, objects)) as client:
            capture(client, out, "health:live", "/health/live")
            capture(client, out, "health:ready", "/health/ready")
            capture(client, out, "version", "/api/v1/version")
    with tempfile.TemporaryDirectory() as tmp:                         # receipt_root configured but gone: a real 503
        receipts = os.path.join(tmp, "receipts")
        os.mkdir(receipts)
        app = app_for(tmp, objects, receipts)                          # settings require the directory at load time
        os.rmdir(receipts)
        with TestClient(app) as client:
            capture(client, out, "health:ready_not_ready", "/health/ready")
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
