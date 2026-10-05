"""Hardening Task 5: mias-ui high availability (two replicas, a PodDisruptionBudget, hostname topology spread).

The UI is stateless, so only its Deployment (replicas, scheduling) and a new PDB change; every other object, including
the API, collector and blackbox-exporter pod templates, Routes, alert rules and storage, renders exactly as in chart
0.4.0. Renders with the local `helm` binary (skipped if absent); no cluster access.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest

from tests.test_helm_mias import API_ROUTE, CHART, HELM, PRE_TASK8, ROUTE_ANNOTATIONS, pre_oauth_route, render
from tests.test_helm_restart_cookie import render_chart

UI_SELECTOR = {"matchLabels": {"app.kubernetes.io/name": "mias-ui"}}
SPREAD = {"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "DoNotSchedule",
          "labelSelector": UI_SELECTOR, "matchLabelKeys": ["pod-template-hash"], "nodeTaintsPolicy": "Honor"}
# Canonical spec hashes (sha256 of sorted JSON, 16 hex) rendered from chart 0.4.0 (main a2edbc2, live revision 24).
BASELINE_SPECS = {
    ("Deployment", "mias-api"): "0c64618fa4634c42",
    ("Deployment", "otel-collector"): "09530183e1358ee9",
    ("Deployment", "blackbox-exporter"): "3501427f7c03d9ae",
    ("Deployment", "mias-publisher"): "4d6737ea0641e063",
    ("Route", "mias-api"): "5631b24915198258",
    ("Route", "mias-ui"): "fd8e941a0e45b443",
    ("Service", "mias-ui"): "365bd4af23b746b7",
    ("Probe", "mias-ui-healthz"): "17c7079d255365dd",
    ("PrometheusRule", "mias-alerts"): "a8c62e36cab81401",
    ("PersistentVolumeClaim", "mias-artifacts"): "1e4a3390f26af2c5",
}
COOKIES = ROUTE_ANNOTATIONS                                 # disable_cookies (Task 2) plus HSTS (Task 7)
# Hardening Task 7 changed the blackbox ConfigMap (CA, verification on); this fallback reproduces the 0.4.0 exporter.
SYNTHETIC_FALLBACK = ("syntheticMonitoring.tls.insecureSkipVerify=true", "syntheticMonitoring.tls.caCert=")


def spec_hash(obj):
    return hashlib.sha256(json.dumps(obj["spec"], sort_keys=True).encode()).hexdigest()[:16]


def matches(selector, labels):
    return all(labels.get(k) == v for k, v in selector["matchLabels"].items())


@unittest.skipUnless(HELM, "helm binary not available")
class UiHighAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render()
        cls.dep = cls.objs[("Deployment", "mias-ui")]
        cls.pod = cls.dep["spec"]["template"]["spec"]
        cls.pod_labels = cls.dep["spec"]["template"]["metadata"]["labels"]
        cls.pdb = cls.objs[("PodDisruptionBudget", "mias-ui")]

    # 1. two UI replicas by default
    def test_default_replicas(self):
        self.assertEqual(self.dep["spec"]["replicas"], 2)

    # 2-5. the PDB: rendered with the UI, minAvailable 1, selecting exactly the UI pods
    def test_pdb(self):
        self.assertEqual((self.pdb["apiVersion"], self.pdb["metadata"]["namespace"]), ("policy/v1", "mias"))
        self.assertEqual(self.pdb["spec"], {"minAvailable": 1, "unhealthyPodEvictionPolicy": "AlwaysAllow",
                                            "selector": UI_SELECTOR})
        self.assertEqual(self.pdb["spec"]["selector"], self.dep["spec"]["selector"])
        self.assertTrue(matches(self.pdb["spec"]["selector"], self.pod_labels))
        for name in ("mias-api", "otel-collector", "mias-publisher", "blackbox-exporter"):
            labels = self.objs[("Deployment", name)]["spec"]["template"]["metadata"]["labels"]
            self.assertFalse(matches(self.pdb["spec"]["selector"], labels), name)

    def test_pdb_absent_without_ui_or_spare_replica(self):
        for sets in (("ui.enabled=false",), ("ui.pdb.enabled=false",), ("ui.replicaCount=1",)):
            objs, _ = render(*sets)
            self.assertNotIn(("PodDisruptionBudget", "mias-ui"), objs, sets)   # 1 replica + minAvailable 1 blocks drains
        objs, _ = render("ui.replicaCount=3")
        self.assertEqual(objs[("PodDisruptionBudget", "mias-ui")]["spec"]["minAvailable"], 1)

    # 6-10. hostname topology spread, maxSkew 1, strict, selecting only UI pods
    def test_topology_spread(self):
        self.assertEqual(self.pod["topologySpreadConstraints"], [SPREAD])
        spread = self.pod["topologySpreadConstraints"][0]
        self.assertEqual(spread["topologyKey"], "kubernetes.io/hostname")
        self.assertEqual(spread["maxSkew"], 1)
        self.assertEqual(spread["whenUnsatisfiable"], "DoNotSchedule")
        self.assertTrue(matches(spread["labelSelector"], self.pod_labels))
        for name in ("mias-api", "otel-collector", "mias-publisher", "blackbox-exporter"):
            labels = self.objs[("Deployment", name)]["spec"]["template"]["metadata"]["labels"]
            self.assertFalse(matches(spread["labelSelector"], labels), name)
        self.assertNotIn("topology.kubernetes.io/zone", self.text)          # the lab nodes have no zone labels
        self.assertNotIn("affinity", self.pod)
        self.assertNotIn("nodeSelector", self.pod)                          # masters stay excluded by their taint

    def test_topology_spread_mode_and_toggle(self):
        objs, _ = render("ui.topologySpread.whenUnsatisfiable=ScheduleAnyway")
        spread = objs[("Deployment", "mias-ui")]["spec"]["template"]["spec"]["topologySpreadConstraints"][0]
        self.assertEqual(spread, dict(SPREAD, whenUnsatisfiable="ScheduleAnyway"))
        with self.assertRaises(AssertionError):
            render("ui.topologySpread.whenUnsatisfiable=Sometimes")
        objs, _ = render("ui.topologySpread.enabled=false")
        self.assertNotIn("topologySpreadConstraints", objs[("Deployment", "mias-ui")]["spec"]["template"]["spec"])

    # 11. rolling updates never drop below the desired Ready count
    def test_rolling_update_is_safe(self):
        self.assertEqual(self.dep["spec"]["strategy"],
                         {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}})
        container = self.pod["containers"][0]
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["httpGet"]["path"], "/healthz", probe)

    # The UI Deployment differs from chart 0.4.0 only by replicas and the spread constraint.
    def test_ui_deployment_change_is_only_ha(self):
        oauth_off, _ = render(*PRE_TASK8)                                     # minus Hardening Task 8's sidecar
        spec = json.loads(json.dumps(oauth_off[("Deployment", "mias-ui")]["spec"]))
        spec["replicas"] = 1
        del spec["template"]["spec"]["topologySpreadConstraints"]
        self.assertEqual(hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16],
                         "e80677852eddace4")                                # the 0.3.1/0.4.0 mias-ui Deployment spec

    # 12-14. API, collector and blackbox (and publisher) pod templates unchanged
    def test_other_workloads_unchanged(self):
        fallback, _ = render(*SYNTHETIC_FALLBACK)
        for name in ("mias-api", "otel-collector", "blackbox-exporter", "mias-publisher"):
            key = ("Deployment", name)
            objs = fallback if name == "blackbox-exporter" else self.objs
            self.assertEqual(spec_hash(objs[key]), BASELINE_SPECS[key], name)

    def test_chart_bump_restarts_no_other_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart = os.path.join(tmp, "mias")
            shutil.copytree(CHART, chart)
            path = os.path.join(chart, "Chart.yaml")
            text = open(path, encoding="utf-8").read()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(re.sub(r"^version: .*$", "version: 9.9.9", text, flags=re.M))
            other = render_chart(chart)
        for name in ("mias-api", "otel-collector", "blackbox-exporter", "mias-publisher", "mias-ui"):
            self.assertEqual(other[("Deployment", name)]["spec"]["template"],
                             self.objs[("Deployment", name)]["spec"]["template"], name)

    # 15-16. Routes unchanged, router cookies still disabled; the UI Service is untouched
    def test_routes_and_cookies_unchanged(self):
        with_api_route, _ = render(API_ROUTE)                                  # the API Route is off since Task 8 Stage 4
        for name in ("mias-api", "mias-ui"):
            route = with_api_route[("Route", name)]                          # minus Tasks 7-8 (cert ref, UI port)
            self.assertEqual(spec_hash(pre_oauth_route(route)), BASELINE_SPECS[("Route", name)], name)
            self.assertEqual(route["metadata"]["annotations"], COOKIES, name)
        oauth_off, _ = render(*PRE_TASK8)                                     # Task 8 adds only the oauth Service port
        service = oauth_off[("Service", "mias-ui")]
        self.assertEqual(spec_hash(service), BASELINE_SPECS[("Service", "mias-ui")])
        self.assertNotIn("sessionAffinity", service["spec"])               # stateless: no affinity anywhere
        self.assertNotIn("sessionAffinity", self.text)

    # 17. alert rules and the synthetic Probe unchanged (MiasUiUnavailable stays "available < 1")
    def test_alerts_and_probe_unchanged(self):
        rule = json.loads(json.dumps(self.objs[("PrometheusRule", "mias-alerts")]))
        for group in rule["spec"]["groups"]:                                    # minus Hardening Task 7's expiry alert
            group["rules"] = [r for r in group["rules"] if r["alert"] != "MiasTlsCertificateExpiring"]
        self.assertEqual(spec_hash(rule), BASELINE_SPECS[("PrometheusRule", "mias-alerts")])
        ui = [r for g in rule["spec"]["groups"] for r in g["rules"] if r["alert"] == "MiasUiUnavailable"][0]
        self.assertIn('kube_deployment_status_replicas_available{namespace="mias",deployment="mias-ui"} < 1',
                      ui["expr"])
        self.assertEqual(spec_hash(self.objs[("Probe", "mias-ui-healthz")]), BASELINE_SPECS[("Probe", "mias-ui-healthz")])

    # 18. storage unchanged; the UI has no persistent volume
    def test_storage_unchanged(self):
        key = ("PersistentVolumeClaim", "mias-artifacts")
        self.assertEqual(spec_hash(self.objs[key]), BASELINE_SPECS[key])
        self.assertEqual([k for k in self.objs if k[0] == "PersistentVolumeClaim"], [key])
        self.assertNotIn("persistentVolumeClaim", json.dumps(self.dep))

    # 19. no PDB for the API (single replica on RWO storage) or any other workload
    def test_only_the_ui_has_a_pdb(self):
        self.assertEqual([k for k in self.objs if k[0] == "PodDisruptionBudget"], [("PodDisruptionBudget", "mias-ui")])
        self.assertEqual(self.objs[("Deployment", "mias-api")]["spec"]["replicas"], 1)

    # 20. ui.enabled=false removes the UI Deployment, Service, Route and PDB cleanly
    def test_ui_disabled_removes_everything_ui(self):
        objs, text = render("ui.enabled=false")
        for key in (("Deployment", "mias-ui"), ("Service", "mias-ui"), ("Route", "mias-ui"),
                    ("PodDisruptionBudget", "mias-ui")):
            self.assertNotIn(key, objs)
        self.assertFalse([k for k in objs if k[0] == "PodDisruptionBudget"])
        self.assertNotIn("mias-ui", text)
        for name in ("mias-api", "otel-collector", "mias-publisher"):
            self.assertEqual(objs[("Deployment", name)], self.objs[("Deployment", name)], name)


if __name__ == "__main__":
    unittest.main()
