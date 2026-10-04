"""Hardening Task 4: synthetic monitoring of the UI Route.

A Prometheus Blackbox Exporter (namespace mias-monitoring) probes https://<ui route>/healthz; UWM scrapes it through a
Probe in the release namespace, and MiasUiSyntheticFailing alerts on probe_success. Everything is additive: no existing
workload, Route or storage object changes. Renders with the local `helm` binary (skipped if absent); no cluster access.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests.test_helm_alerts import CORE, EXPECTED as ALERT_SPECS
from tests.test_helm_mias import CHART, HELM, SYNTHETIC_EXPORTER, SYNTHETIC_NS, SYNTHETIC_OBJECTS, render
from tests.test_helm_restart_cookie import gojson_sha, render_chart
from tests.test_openshift_manifests import parse

TARGET = "https://mias-ui.apps.ngc.sirii.org/healthz"
EXPORTER_IMAGE = ("image-registry.openshift-image-registry.svc:5000/mias-monitoring/blackbox-exporter"
                  "@sha256:22def1f1843206a9dc6bf476533c16c025d0594ad2867778368a1735b87c7d38")   # v0.28.0, mirrored
# Canonical spec hashes (sha256 of sorted JSON, 16 hex) rendered from chart 0.3.1 (main 5d1befe, live revision 22).
# Task 4 must not change any of them: no rollout of an existing workload, no Route or PVC change.
BASELINE_SPECS = {
    ("Deployment", "mias-api"): "0c64618fa4634c42",
    ("Deployment", "mias-ui"): "e80677852eddace4",
    ("Deployment", "otel-collector"): "09530183e1358ee9",
    ("Deployment", "mias-publisher"): "4d6737ea0641e063",
    ("Route", "mias-api"): "5631b24915198258",
    ("Route", "mias-ui"): "fd8e941a0e45b443",
    ("PersistentVolumeClaim", "mias-artifacts"): "1e4a3390f26af2c5",
}


def spec_hash(obj):
    return hashlib.sha256(json.dumps(obj["spec"], sort_keys=True).encode()).hexdigest()[:16]


def alert_rules(objs):
    rule = objs.get(("PrometheusRule", "mias-alerts"))
    return {} if rule is None else {r["alert"]: r for g in rule["spec"]["groups"] for r in g["rules"]}


@unittest.skipUnless(HELM, "helm binary not available")
class SyntheticMonitoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render()
        cls.off, cls.off_text = render("syntheticMonitoring.enabled=false")
        cls.dep = cls.objs[("Deployment", "blackbox-exporter")]
        cls.pod = cls.dep["spec"]["template"]["spec"]
        cls.c = cls.pod["containers"][0]
        cls.probe = cls.objs[("Probe", "mias-ui-healthz")]
        cls.config = cls.objs[("ConfigMap", "blackbox-exporter-config")]["data"]["config.yml"]

    # 1. renders when enabled (the default)
    def test_resources_render_when_enabled(self):
        self.assertLessEqual(SYNTHETIC_OBJECTS, set(self.objs))
        for key in SYNTHETIC_EXPORTER:
            self.assertEqual(self.objs[key]["metadata"]["namespace"], SYNTHETIC_NS, key)
            self.assertEqual(self.objs[key]["metadata"]["labels"]["app.kubernetes.io/part-of"], "mias", key)
        # The Probe must be in the release namespace: UWM enforces namespace="mias" on the series the rules see.
        self.assertEqual(self.probe["metadata"]["namespace"], "mias")
        self.assertEqual(self.probe["apiVersion"], "monitoring.coreos.com/v1")
        self.assertNotIn(("Namespace", SYNTHETIC_NS), self.objs)        # created out of band (a prerequisite)

    # 2. the disabled path removes exactly the synthetic objects and the synthetic alert
    def test_disabled_path_removes_them_cleanly(self):
        self.assertEqual(set(self.objs) - set(self.off), SYNTHETIC_OBJECTS)
        self.assertFalse(set(self.off) - set(self.objs))
        for key, obj in self.off.items():
            if key != ("PrometheusRule", "mias-alerts"):
                self.assertEqual(self.objs[key], obj, key)
        for word in ("blackbox", "mias-monitoring", "probe_success", "MiasUiSyntheticFailing", "mias-ui-healthz"):
            self.assertNotIn(word, self.off_text)
        # It also follows the UI and its Route: without a UI Route there is nothing to probe.
        for sets in (("ui.enabled=false",), ("ui.route.enabled=false",)):
            objs, text = render(*sets)
            self.assertFalse(SYNTHETIC_OBJECTS & set(objs), sets)
            self.assertNotIn("MiasUiSyntheticFailing", text, sets)

    # 3. the probe target is exactly the UI Route's health endpoint, through the exporter Service
    def test_probe_target_is_exact(self):
        spec = self.probe["spec"]
        self.assertEqual(spec["targets"], {"staticConfig": {"static": [TARGET]}})
        self.assertEqual(spec["prober"], {"url": f"blackbox-exporter.{SYNTHETIC_NS}.svc:9115", "scheme": "http",
                                          "path": "/probe"})
        self.assertEqual(spec["jobName"], "mias-ui-synthetic")
        route = self.objs[("Route", "mias-ui")]["spec"]
        self.assertEqual(TARGET, f"https://{route['host']}/healthz")
        objs, _ = render("ui.route.host=dash.example.test")
        self.assertEqual(objs[("Probe", "mias-ui-healthz")]["spec"]["targets"]["staticConfig"]["static"],
                         ["https://dash.example.test/healthz"])
        service = self.objs[("Service", "blackbox-exporter")]["spec"]
        self.assertEqual((service["type"], service["ports"], service["selector"]),
                         ("ClusterIP", [{"name": "http", "port": 9115, "targetPort": "http", "protocol": "TCP"}],
                          {"app.kubernetes.io/name": "blackbox-exporter"}))

    # 4. the module name matches between the Probe and the exporter config, and the module is strict
    def test_module(self):
        self.assertEqual(self.probe["spec"]["module"], "http_2xx_mias")
        module = parse(self.config)["modules"]
        self.assertEqual(list(module), ["http_2xx_mias"])
        http = module["http_2xx_mias"]["http"]
        self.assertEqual(module["http_2xx_mias"]["prober"], "http")
        self.assertEqual(http["method"], "GET")
        self.assertEqual(http["follow_redirects"], False)
        self.assertEqual(http["fail_if_not_ssl"], True)
        self.assertEqual(http["preferred_ip_protocol"], "ip4")
        self.assertEqual(http["valid_status_codes"], [200])
        self.assertEqual(http["valid_http_versions"], ["HTTP/1.1", "HTTP/2.0"])
        self.assertEqual(http["fail_if_body_not_matches_regexp"], ["^ok"])     # the UI's /healthz body is "ok"

    # 5. interval and timeouts are sane: probe timeout < scrape timeout < interval
    def test_interval_and_timeouts(self):
        seconds = lambda value: int(re.fullmatch(r"(\d+)s", value).group(1))
        interval, scrape = seconds(self.probe["spec"]["interval"]), seconds(self.probe["spec"]["scrapeTimeout"])
        probe = seconds(parse(self.config)["modules"]["http_2xx_mias"]["timeout"])
        self.assertEqual((interval, scrape, probe), (30, 15, 10))
        self.assertLess(probe, scrape)
        self.assertLess(scrape, interval)
        self.assertLessEqual(interval * 10, 300)        # at least ten probes inside the alert's 5m window

    # 6. TLS verification is skipped only because of the lab's self-signed wildcard, and only via the value
    def test_tls_skip_verify_follows_the_lab_value(self):
        tls = parse(self.config)["modules"]["http_2xx_mias"]["http"]["tls_config"]
        self.assertEqual(tls, {"insecure_skip_verify": True})
        objs, _ = render("syntheticMonitoring.tls.insecureSkipVerify=false")
        strict = objs[("ConfigMap", "blackbox-exporter-config")]["data"]["config.yml"]
        self.assertEqual(parse(strict)["modules"]["http_2xx_mias"]["http"]["tls_config"],
                         {"insecure_skip_verify": False})
        # The skip lives only in the exporter: no CA or TLS setting reaches any MIAS application container or Route.
        self.assertEqual(self.text.count("insecure_skip_verify"), 1)
        values = open(os.path.join(CHART, "values.yaml"), encoding="utf-8").read()
        self.assertRegex(values, r"# TEMPORARY: the lab ingress certificate is self-signed.*\n\s+insecureSkipVerify: true")

    # 7. restricted-v2 compatible (arbitrary UID, no privilege, read-only root, digest-pinned image)
    def test_deployment_is_restricted(self):
        self.assertEqual(self.pod["securityContext"], {"runAsNonRoot": True, "fsGroupChangePolicy": "OnRootMismatch",
                                                       "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(self.c["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                     "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]},
                                                     "seccompProfile": {"type": "RuntimeDefault"}})
        for forbidden in ("runAsUser", "runAsGroup", "fsGroup", "hostNetwork", "hostPID", "hostIPC", "privileged",
                          "hostPort", "hostPath"):
            self.assertNotIn(f'"{forbidden}"', json.dumps(self.dep))     # OpenShift assigns the UID and fsGroup
        self.assertEqual(self.c["image"], EXPORTER_IMAGE)
        self.assertEqual(self.dep["spec"]["replicas"], 1)
        self.assertEqual(self.c["args"], ["--config.file=/etc/blackbox_exporter/config.yml", "--web.listen-address=:9115"])
        self.assertEqual(self.c["volumeMounts"], [{"name": "config", "mountPath": "/etc/blackbox_exporter",
                                                   "readOnly": True}])
        for probe in ("livenessProbe", "readinessProbe"):
            self.assertEqual(self.c[probe]["httpGet"], {"path": "/-/healthy", "port": "http"})
        for bad in ("latest", "sha256:abc", ""):
            with self.assertRaises(AssertionError):
                render(f"syntheticMonitoring.image.digest={bad}")

    # 8. no service-account token: the exporter never talks to the Kubernetes API
    def test_no_service_account_token(self):
        self.assertEqual(self.objs[("ServiceAccount", "blackbox-exporter")]["automountServiceAccountToken"], False)
        self.assertEqual((self.pod["serviceAccountName"], self.pod["automountServiceAccountToken"],
                          self.pod["enableServiceLinks"]), ("blackbox-exporter", False, False))
        for banned in ("Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding", "Secret"):
            self.assertNotIn(banned, {k for k, _ in self.objs})

    # 9. resources are requested and limited, and small
    def test_resources_defined(self):
        self.assertEqual(self.c["resources"], {"requests": {"cpu": "10m", "memory": "16Mi"},
                                               "limits": {"cpu": "100m", "memory": "64Mi"}})

    # 10. no storage: only the read-only config volume
    def test_no_pvc(self):
        self.assertEqual(self.pod["volumes"], [{"name": "config", "configMap": {"name": "blackbox-exporter-config"}}])
        self.assertEqual([k for k in self.objs if k[0] == "PersistentVolumeClaim"],
                         [("PersistentVolumeClaim", "mias-artifacts")])
        self.assertNotIn("emptyDir", json.dumps(self.dep))

    # 11. minimal NetworkPolicy: default deny in the namespace; in from UWM Prometheus on 9115; out to DNS and the
    # ingress VIP /32 on 443 only
    def test_network_policy_minimal(self):
        deny = self.objs[("NetworkPolicy", "synthetic-default-deny")]["spec"]
        self.assertEqual(deny, {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
        policy = self.objs[("NetworkPolicy", "blackbox-exporter")]["spec"]
        self.assertEqual(policy["podSelector"], {"matchLabels": {"app.kubernetes.io/name": "blackbox-exporter"}})
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["ingress"], [{"from": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-user-workload-monitoring"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}}}],
            "ports": [{"protocol": "TCP", "port": 9115}]}])
        self.assertEqual(policy["egress"], [
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}},
                     "podSelector": {"matchLabels": {"dns.operator.openshift.io/daemonset-dns": "default"}}}],
             "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}]},
            {"to": [{"ipBlock": {"cidr": "192.168.5.141/32"}}], "ports": [{"protocol": "TCP", "port": 443}]}])
        self.assertNotIn("0.0.0.0/0", self.text)
        self.assertNotIn("except", json.dumps(policy))
        # The MIAS policies are untouched, and the global switch drops the synthetic policies too.
        objs, _ = render("networkPolicy.enabled=false")
        self.assertFalse([k for k in objs if k[0] == "NetworkPolicy"])
        self.assertIn(("Deployment", "blackbox-exporter"), objs)

    # 12. the alert exists only while synthetic monitoring is active
    def test_alert_only_when_active(self):
        rule = alert_rules(self.objs)["MiasUiSyntheticFailing"]
        self.assertEqual(rule["for"], "5m")
        self.assertEqual(rule["labels"], {"severity": "critical", "component": "ui", "part_of": "mias"})
        self.assertEqual(set(rule["annotations"]), {"summary", "description", "runbook_hint"})
        self.assertIn('probe_success{namespace="mias",job="mias-ui-synthetic"} == 0', rule["expr"])
        self.assertIn('up{namespace="mias",job="mias-ui-synthetic"} == 0', rule["expr"])   # probe could not run
        group = [g for g in self.objs[("PrometheusRule", "mias-alerts")]["spec"]["groups"]
                 if g["name"] == "mias.synthetic"]
        self.assertEqual([[r["alert"] for r in g["rules"]] for g in group], [["MiasUiSyntheticFailing"]])
        for sets in (("syntheticMonitoring.enabled=false",), ("ui.enabled=false",), ("ui.route.enabled=false",)):
            objs, _ = render(*sets)
            self.assertNotIn("MiasUiSyntheticFailing", alert_rules(objs), sets)
        objs, _ = render("monitoring.alerts.enabled=false")
        self.assertNotIn(("PrometheusRule", "mias-alerts"), objs)
        self.assertIn(("Probe", "mias-ui-healthz"), objs)                       # probing does not need the alert

    # 13. the existing 11 alerts are byte-for-byte unchanged
    def test_existing_alerts_unchanged(self):
        self.assertEqual(len(CORE), 11)
        on, off = alert_rules(self.objs), alert_rules(self.off)
        self.assertEqual(set(off), CORE)
        self.assertEqual(set(on), CORE | {"MiasUiSyntheticFailing"})
        self.assertEqual(set(ALERT_SPECS), set(on))
        for name in CORE:
            self.assertEqual(on[name], off[name], name)
        groups = lambda objs: [g for g in objs[("PrometheusRule", "mias-alerts")]["spec"]["groups"]
                               if g["name"] != "mias.synthetic"]
        self.assertEqual(groups(self.objs), groups(self.off))

    # 14. a chart version bump alone restarts nothing, including the exporter
    def test_chart_version_bump_restarts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart = os.path.join(tmp, "mias")
            shutil.copytree(CHART, chart)
            path = os.path.join(chart, "Chart.yaml")
            text = open(path, encoding="utf-8").read()
            bumped = re.sub(r"^version: .*$", "version: 9.9.9", text, flags=re.M)
            self.assertNotEqual(bumped, text)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(bumped)
            other = render_chart(chart)
        self.assertEqual(other[("Deployment", "blackbox-exporter")]["metadata"]["labels"]["helm.sh/chart"], "mias-9.9.9")
        for name in ("mias-api", "mias-ui", "otel-collector", "mias-publisher", "blackbox-exporter"):
            self.assertEqual(other[("Deployment", name)]["spec"]["template"],
                             self.objs[("Deployment", name)]["spec"]["template"], name)
        # The exporter checksum hashes its ConfigMap data only, and changes only with real probe configuration.
        annotations = self.dep["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations["checksum/config"],
                         gojson_sha(self.objs[("ConfigMap", "blackbox-exporter-config")]["data"]))
        for change, rolls in (("syntheticMonitoring.probeTimeout=8s", True),
                              ("syntheticMonitoring.tls.insecureSkipVerify=false", True),
                              ("syntheticMonitoring.interval=60s", False),
                              ("ui.replicaCount=2", False)):
            objs, _ = render(change)
            changed = objs[("Deployment", "blackbox-exporter")]["spec"]["template"] != self.dep["spec"]["template"]
            self.assertEqual(changed, rolls, change)

    # 15. no API/UI/collector/publisher pod-template (or any Deployment spec) change versus chart 0.3.1
    def test_no_existing_workload_diff(self):
        for name in ("mias-api", "mias-ui", "otel-collector", "mias-publisher"):
            key = ("Deployment", name)
            self.assertEqual(spec_hash(self.objs[key]), BASELINE_SPECS[key], name)
            self.assertEqual(self.objs[key], self.off[key], name)
        for key in (("ConfigMap", "mias-api-config"), ("ConfigMap", "otel-collector-config"),
                    ("ConfigMap", "mias-ui-config"), ("ConfigMap", "mias-publisher-config")):
            self.assertEqual(self.objs[key], self.off[key], key)

    # 16. no Route change (host, TLS, cookies, target)
    def test_no_route_changes(self):
        for name in ("mias-api", "mias-ui"):
            key = ("Route", name)
            self.assertEqual(spec_hash(self.objs[key]), BASELINE_SPECS[key], name)
            self.assertEqual(self.objs[key]["metadata"]["annotations"],
                             {"haproxy.router.openshift.io/disable_cookies": "true"}, name)
        self.assertEqual([k for k in self.objs if k[0] == "Route"], [k for k in self.off if k[0] == "Route"])

    # 17. no storage change (the artifact PVC keeps its spec and keep policy)
    def test_no_storage_changes(self):
        key = ("PersistentVolumeClaim", "mias-artifacts")
        self.assertEqual(spec_hash(self.objs[key]), BASELINE_SPECS[key])
        self.assertEqual(self.objs[key], self.off[key])
        self.assertEqual(self.objs[key]["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertNotIn("PersistentVolume", {k for k, _ in self.objs})
        self.assertNotIn("volumeClaimTemplates", self.text.split("mias-monitoring", 1)[-1])

    def test_lint_and_chart_version(self):
        result = subprocess.run([HELM, "lint", CHART, "--namespace", "mias"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        chart = parse(open(os.path.join(CHART, "Chart.yaml"), encoding="utf-8").read())
        self.assertEqual((chart["version"], chart["appVersion"]), ("0.4.0", "259236684a74"))


if __name__ == "__main__":
    unittest.main()
