"""Hardening Task 7: trusted Route TLS, HSTS and verified synthetic TLS.

Both Routes serve MIAS lab certificates from out-of-band Secrets (spec.tls.externalCertificate, readable by the router
through a narrow Role), send HSTS, and keep edge TLS, the redirect and disabled cookies. The probe verifies TLS
against the public lab CA. Nothing else changes. Renders with the local `helm` binary (skipped if absent); offline.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import unittest

from tests.test_helm_mias import CHART, HELM, HSTS, ROOT, ROUTE_TLS_RBAC, render, route_tls
from tests.test_openshift_manifests import parse

CA_FILE = os.path.join(ROOT, "docs", "tls", "mias-lab-ca.crt")
HOSTS = {"mias-api": "mias-api.apps.ngc.sirii.org", "mias-ui": "mias-ui.apps.ngc.sirii.org"}
# Canonical spec hashes from chart 0.4.1 (main 225f98e, live revision 25), for objects Task 7 must not change.
BASELINE_SPECS = {
    ("Deployment", "mias-api"): "0c64618fa4634c42",
    ("Deployment", "otel-collector"): "09530183e1358ee9",
    ("Deployment", "mias-publisher"): "4d6737ea0641e063",
    ("Route", "mias-api"): "5631b24915198258",
    ("Route", "mias-ui"): "fd8e941a0e45b443",
    ("PersistentVolumeClaim", "mias-artifacts"): "1e4a3390f26af2c5",
}
SYNTHETIC_FALLBACK = ("syntheticMonitoring.tls.insecureSkipVerify=true", "syntheticMonitoring.tls.caCert=")


def spec_hash(obj):
    return hashlib.sha256(json.dumps(obj["spec"], sort_keys=True).encode()).hexdigest()[:16]


def pem_der(text):
    body = "".join(line for line in text.strip().splitlines() if not line.startswith("-----"))
    return base64.b64decode(body)


@unittest.skipUnless(HELM, "helm binary not available")
class RouteTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render()
        cls.routes = {name: cls.objs[("Route", name)] for name in HOSTS}

    # 1. HSTS renders exactly, on both Routes; no includeSubDomains, no preload
    def test_hsts_annotation(self):
        for name, route in self.routes.items():
            self.assertEqual(route["metadata"]["annotations"]["haproxy.router.openshift.io/hsts_header"], HSTS, name)
        self.assertRegex(HSTS, r"^max-age=\d+$")
        self.assertNotIn("includeSubDomains", self.text)
        self.assertNotIn("preload", self.text)
        objs, _ = render("routeTLS.hsts=")
        for name in HOSTS:
            self.assertNotIn("haproxy.router.openshift.io/hsts_header", objs[("Route", name)]["metadata"]["annotations"])

    # 2-5. redirect kept, cookies still disabled, hosts unchanged, edge termination; only the cert reference is new
    def test_route_semantics_preserved(self):
        for name, route in self.routes.items():
            self.assertEqual(route["spec"]["tls"], route_tls(f"{name}-tls"), name)
            self.assertEqual(route["spec"]["host"], HOSTS[name], name)
            self.assertEqual(route["metadata"]["annotations"]["haproxy.router.openshift.io/disable_cookies"], "true")
            self.assertEqual(set(route["metadata"]["annotations"]),
                             {"haproxy.router.openshift.io/disable_cookies", "haproxy.router.openshift.io/hsts_header"})
            stripped = json.loads(json.dumps(route))
            del stripped["spec"]["tls"]["externalCertificate"]
            self.assertEqual(spec_hash(stripped), BASELINE_SPECS[("Route", name)], name)   # target, port, wildcard
            self.assertNotIn("certificate", route["spec"]["tls"])           # no inline cert/key on the Route
            self.assertNotIn("key", route["spec"]["tls"])
        objs, _ = render("route.tls.externalCertificateSecret=", "ui.route.tls.externalCertificateSecret=")
        for name in HOSTS:                                                  # fallback: the ingress default cert
            self.assertEqual(objs[("Route", name)]["spec"]["tls"],
                             {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"})
        self.assertFalse(ROUTE_TLS_RBAC & set(objs))

    def test_router_may_read_only_the_two_tls_secrets(self):
        self.assertLessEqual(ROUTE_TLS_RBAC, set(self.objs))
        role = self.objs[("Role", "mias-route-tls-reader")]
        self.assertEqual(role["metadata"]["namespace"], "mias")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"],
                                          "resourceNames": ["mias-api-tls", "mias-ui-tls"],
                                          "verbs": ["get", "list", "watch"]}])
        binding = self.objs[("RoleBinding", "mias-route-tls-reader")]
        self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                                              "name": "mias-route-tls-reader"})
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "router",
                                                "namespace": "openshift-ingress"}])
        for kind in ("ClusterRole", "ClusterRoleBinding", "Secret"):
            self.assertNotIn(kind, {k for k, _ in self.objs})
        objs, _ = render("ui.enabled=false")                               # the UI cert grant follows the UI Route
        self.assertEqual(objs[("Role", "mias-route-tls-reader")]["rules"][0]["resourceNames"], ["mias-api-tls"])
        objs, _ = render("ui.route.enabled=false", "route.enabled=false")
        self.assertFalse(ROUTE_TLS_RBAC & set(objs))

    # 6. the probe verifies TLS normally, against the public MIAS lab CA
    def test_synthetic_verifies_tls(self):
        data = self.objs[("ConfigMap", "blackbox-exporter-config")]["data"]
        tls = parse(data["config.yml"])["modules"]["http_2xx_mias"]["http"]["tls_config"]
        self.assertEqual(tls, {"insecure_skip_verify": False, "ca_file": "/etc/blackbox_exporter/ca.crt"})
        self.assertNotIn("insecure_skip_verify: true", self.text)
        with open(CA_FILE, encoding="ascii") as handle:
            ca = handle.read()
        self.assertEqual(data["ca.crt"], ca)                                # the committed public CA certificate
        self.assertTrue(ca.startswith("-----BEGIN CERTIFICATE-----\n"))
        self.assertEqual(ca.count("BEGIN CERTIFICATE"), 1)
        mount = self.objs[("Deployment", "blackbox-exporter")]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(mount["volumeMounts"], [{"name": "config", "mountPath": "/etc/blackbox_exporter",
                                                  "readOnly": True}])

    # 7. no private-key material in the chart, values, docs/tls or anywhere tracked
    def test_no_private_key_material(self):
        markers = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
        self.assertNotRegex(self.text, markers)
        for directory, _, files in os.walk(CHART):
            for name in files:
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    self.assertNotRegex(handle.read(), markers, name)
        self.assertEqual(sorted(os.listdir(os.path.dirname(CA_FILE))), ["mias-lab-ca.crt"])
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if tracked.returncode == 0:
            for path in tracked.stdout.split():
                self.assertFalse(path.endswith((".key", "tls.key")) or "mias-tls" in path, path)

    def test_lab_ca_is_name_constrained_public_certificate(self):
        with open(CA_FILE, encoding="ascii") as handle:
            der = pem_der(handle.read())
        self.assertIn(b"MIAS Lab Root CA 2026", der)
        for host in HOSTS.values():                                         # nameConstraints: the two hosts only
            self.assertIn(host.encode(), der)
        self.assertNotIn(b"*.apps", der)

    # 8-11. workloads: API, UI, collector and publisher pod templates unchanged; blackbox only via its ConfigMap
    def test_workloads_unchanged_except_blackbox_config(self):
        for name in ("mias-api", "otel-collector", "mias-publisher"):
            key = ("Deployment", name)
            self.assertEqual(spec_hash(self.objs[key]), BASELINE_SPECS[key], name)
        fallback, _ = render(*SYNTHETIC_FALLBACK)
        self.assertEqual(self.objs[("Deployment", "mias-ui")], fallback[("Deployment", "mias-ui")])
        blackbox = self.objs[("Deployment", "blackbox-exporter")]["spec"]["template"]
        old = fallback[("Deployment", "blackbox-exporter")]["spec"]["template"]
        self.assertNotEqual(blackbox["metadata"]["annotations"]["checksum/config"],
                            old["metadata"]["annotations"]["checksum/config"])   # one planned exporter restart
        blackbox, old = json.loads(json.dumps(blackbox)), json.loads(json.dumps(old))
        blackbox["metadata"]["annotations"].pop("checksum/config")
        old["metadata"]["annotations"].pop("checksum/config")
        self.assertEqual(blackbox, old)                                     # and nothing else in its pod template
        for name in ("mias-api", "mias-ui", "otel-collector", "mias-publisher"):
            objs, _ = render("routeTLS.hsts=max-age=31536000")
            self.assertEqual(objs[("Deployment", name)], self.objs[("Deployment", name)], name)   # HSTS: Route only

    # 12. PDB and topology spread unchanged
    def test_ui_ha_unchanged(self):
        self.assertEqual(self.objs[("PodDisruptionBudget", "mias-ui")]["spec"]["minAvailable"], 1)
        spread = self.objs[("Deployment", "mias-ui")]["spec"]["template"]["spec"]["topologySpreadConstraints"]
        self.assertEqual([s["topologyKey"] for s in spread], ["kubernetes.io/hostname"])
        self.assertEqual(self.objs[("Deployment", "mias-ui")]["spec"]["replicas"], 2)

    # 13. PrometheusRule: only the new certificate-expiry alert
    def test_certificate_expiry_alert(self):
        rule = self.objs[("PrometheusRule", "mias-alerts")]
        alerts = {r["alert"]: r for g in rule["spec"]["groups"] for r in g["rules"]}
        expiry = alerts["MiasTlsCertificateExpiring"]
        self.assertEqual(expiry["expr"].strip(), '(probe_ssl_earliest_cert_expiry{namespace="mias",'
                                                 'job="mias-ui-synthetic"} - time()) < 30 * 86400')
        self.assertEqual((expiry["for"], expiry["labels"]),
                         ("1h", {"severity": "warning", "component": "tls", "part_of": "mias"}))
        self.assertEqual(set(expiry["annotations"]), {"summary", "description", "runbook_hint"})
        self.assertEqual(len(alerts), 13)
        objs, _ = render("syntheticMonitoring.enabled=false")
        names = {r["alert"] for g in objs[("PrometheusRule", "mias-alerts")]["spec"]["groups"] for r in g["rules"]}
        self.assertNotIn("MiasTlsCertificateExpiring", names)              # it needs the probe's metric

    # 14. storage unchanged
    def test_storage_unchanged(self):
        key = ("PersistentVolumeClaim", "mias-artifacts")
        self.assertEqual(spec_hash(self.objs[key]), BASELINE_SPECS[key])
        self.assertEqual([k for k in self.objs if k[0] == "PersistentVolumeClaim"], [key])

    # 15. ui.enabled=false still removes everything UI (Route, PDB, probe) and keeps the API Route's TLS and HSTS
    def test_ui_disabled(self):
        objs, text = render("ui.enabled=false")
        self.assertNotIn("mias-ui", text)
        self.assertNotIn(("Route", "mias-ui"), objs)
        self.assertNotIn(("PodDisruptionBudget", "mias-ui"), objs)
        self.assertEqual(objs[("Route", "mias-api")], self.routes["mias-api"])

    def test_chart_version(self):
        with open(os.path.join(CHART, "Chart.yaml"), encoding="utf-8") as handle:
            chart = parse(handle.read())
        self.assertEqual((chart["version"], chart["appVersion"]), ("0.5.0", "259236684a74"))


if __name__ == "__main__":
    unittest.main()
