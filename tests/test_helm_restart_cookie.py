"""Hardening Task 2: Helm restart hygiene and router cookies.

- mias-api and otel-collector roll only when their ConfigMap *data* changes: chart metadata (labels, chart version)
  and unrelated values must not change checksum/config.
- Both Routes disable the OpenShift router's sticky cookie, with host, TLS and target unchanged.
Renders with the local `helm` binary (skipped if absent); no cluster access.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests.test_helm_mias import API_ROUTE, CHART, HELM, ROUTE_ANNOTATIONS, UI_ROUTE_PORT, render, route_tls
from tests.test_openshift_manifests import parse


def render_chart(chart, *sets):
    args = [HELM, "template", "mias", chart, "--namespace", "mias"]
    for item in sets:
        args += ["--set", item]
    out = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout
    docs = [parse(c) for c in re.split(r"^---\s*$", out, flags=re.M)
            if any(line.strip() and not line.lstrip().startswith("#") for line in c.splitlines())]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def checksum(objs, deployment):
    return objs[("Deployment", deployment)]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]


def gojson_sha(value):
    """sha256 of Helm's toJson (sorted keys, compact) of a value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@unittest.skipUnless(HELM, "helm binary not available")
class ChecksumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, _ = render()

    def checksums(self, *sets):
        objs, _ = render(*sets)
        return checksum(objs, "mias-api"), checksum(objs, "otel-collector"), checksum(objs, "mias-ui")

    def test_checksums_hash_configmap_data_only(self):
        for deployment, configmap in (("mias-api", "mias-api-config"), ("otel-collector", "otel-collector-config"),):
            self.assertEqual(checksum(self.base, deployment),
                             gojson_sha(self.base[("ConfigMap", configmap)]["data"]), deployment)

    def test_chart_metadata_change_alone_restarts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart = os.path.join(tmp, "mias")
            shutil.copytree(CHART, chart)
            path = os.path.join(chart, "Chart.yaml")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            bumped = re.sub(r"^version: .*$", "version: 9.9.9", text, flags=re.M)
            self.assertNotEqual(bumped, text)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(bumped)
            other = render_chart(chart)
        self.assertIn("mias-9.9.9", other[("ConfigMap", "mias-api-config")]["metadata"]["labels"]["helm.sh/chart"])
        for deployment in ("mias-api", "otel-collector", "mias-ui"):
            self.assertEqual(checksum(other, deployment), checksum(self.base, deployment), deployment)
            # The whole pod template is unchanged too, so no rollout is triggered at all.
            self.assertEqual(other[("Deployment", deployment)]["spec"]["template"],
                             self.base[("Deployment", deployment)]["spec"]["template"], deployment)

    def test_real_api_config_changes_roll_the_api(self):
        api, collector, ui = self.checksums()
        for change in ("api.docsEnabled=true", "observability.otlp.endpoint=http://otel.example.internal:4318",
                       "observability.metrics.exportIntervalMs=30000", "observability.traces.enabled=false"):
            api2, collector2, ui2 = self.checksums(change)
            self.assertNotEqual(api2, api, change)
            self.assertEqual((collector2, ui2), (collector, ui), change)   # API-only values do not roll the collector

    def test_unrelated_values_do_not_roll_the_api(self):
        api, collector, _ = self.checksums()
        for change in ("ui.route.host=ui.example.internal", "ui.replicaCount=2", "monitoring.alerts.enabled=false",
                       "observability.collector.serviceMonitor.interval=60s", "route.host=api.example.internal",
                       "publisher.replicaCount=1"):
            api2, collector2, _ = self.checksums(change)
            self.assertEqual((api2, collector2), (api, collector), change)

    def test_real_collector_config_change_rolls_only_the_collector(self):
        api, collector, ui = self.checksums()
        api2, collector2, ui2 = self.checksums("observability.collector.traceDebugVerbosity=detailed")
        self.assertNotEqual(collector2, collector)
        self.assertEqual((api2, ui2), (api, ui))

    def test_collector_configmap_unchanged(self):
        config = self.base[("ConfigMap", "otel-collector-config")]["data"]["config.yaml"]
        self.assertTrue(config.endswith("\n") and not config.endswith("\n\n"))
        for required in ("endpoint: 0.0.0.0:4318", "endpoint: 0.0.0.0:8889", "verbosity: basic", "level: none"):
            self.assertIn(required, config)


@unittest.skipUnless(HELM, "helm binary not available")
class RouteCookieTests(unittest.TestCase):
    COOKIE = ROUTE_ANNOTATIONS                         # disable_cookies (Task 2) plus HSTS (Task 7)

    def test_both_routes_disable_the_router_cookie_and_keep_their_spec(self):
        objs, _ = render(API_ROUTE)                    # the API Route is off by default since Task 8 Stage 4
        api = objs[("Route", "mias-api")]
        ui = objs[("Route", "mias-ui")]
        for route in (api, ui):
            self.assertEqual(route["metadata"]["annotations"], self.COOKIE)
            self.assertEqual(route["spec"]["tls"], route_tls(route["metadata"]["name"] + "-tls"))
            port = UI_ROUTE_PORT if route["metadata"]["name"] == "mias-ui" else "http"   # Task 8 Stage 2
            self.assertEqual(route["spec"]["port"], {"targetPort": port})
            self.assertEqual(route["spec"]["wildcardPolicy"], "None")
        self.assertEqual((api["spec"]["host"], api["spec"]["to"]["name"]), ("mias-api.apps.ngc.sirii.org", "mias-api"))
        self.assertEqual((ui["spec"]["host"], ui["spec"]["to"]["name"]), ("mias-ui.apps.ngc.sirii.org", "mias-ui"))

    def test_route_toggles_still_work(self):
        objs, _ = render(API_ROUTE, "ui.enabled=false")
        self.assertNotIn(("Route", "mias-ui"), objs)
        self.assertEqual(objs[("Route", "mias-api")]["metadata"]["annotations"], self.COOKIE)
        objs, _ = render(API_ROUTE, "ui.route.enabled=false")
        self.assertNotIn(("Route", "mias-ui"), objs)
        self.assertIn(("Route", "mias-api"), objs)
        objs, _ = render("route.enabled=false")
        self.assertNotIn(("Route", "mias-api"), objs)
        self.assertEqual(objs[("Route", "mias-ui")]["metadata"]["annotations"], self.COOKIE)


if __name__ == "__main__":
    unittest.main()
