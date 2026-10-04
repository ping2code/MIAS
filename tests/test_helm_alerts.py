"""Hardening Task 1: the mias-alerts PrometheusRule (User Workload Monitoring). Renders with the local `helm` binary
(skipped if absent) and parses the result structurally; no cluster access. The rule must be additive: enabling or
disabling it changes no other object."""
import re
import unittest

from tests.test_helm_mias import HELM, render

EXPECTED = {
    # alert: (severity, for, component)
    "MiasApiUnavailable": ("critical", "5m", "api"),
    "MiasApiNotReady": ("critical", "5m", "api"),
    "MiasUiUnavailable": ("warning", "5m", "ui"),
    "MiasCollectorDown": ("warning", "10m", "collector"),
    "MiasPodRestarting": ("warning", "10m", "workload"),
    "MiasPublisherLeftRunning": ("info", "2h", "publisher"),
    "MiasArtifactIndexUnhealthy": ("critical", "10m", "artifact-index"),
    "MiasArtifactIndexStale": ("warning", "10m", "artifact-index"),
    "MiasArtifactPvcFilling": ("warning", "30m", "storage"),
    "MiasApi5xxRatio": ("warning", "10m", "api"),
    "MiasTelemetryPipelineStalled": ("warning", None, "telemetry"),
}
# Every metric the rules may use; each was verified to exist in UWM/Thanos with these label shapes.
ALLOWED_METRICS = {
    "kube_deployment_status_replicas_available", "kube_deployment_spec_replicas", "kube_pod_status_ready",
    "kube_pod_container_status_restarts_total", "kube_pod_container_status_waiting_reason",
    "kubelet_volume_stats_used_bytes", "kubelet_volume_stats_capacity_bytes", "up",
    "mias_artifact_index_healthy", "mias_artifact_index_last_success_age_seconds",
    "http_server_request_duration_seconds_count",
}
PROMQL_WORDS = {"sum", "rate", "increase", "max", "by", "or", "and", "absent", "absent_over_time", "vector"}


def metric_names(expr):
    """Identifiers directly followed by a selector or range (metric names), ignoring functions/keywords."""
    names = set(re.findall(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{", expr))
    return names - PROMQL_WORDS


@unittest.skipUnless(HELM, "helm binary not available")
class HelmAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render()
        cls.rule = cls.objs[("PrometheusRule", "mias-alerts")]
        cls.alerts = {r["alert"]: r for g in cls.rule["spec"]["groups"] for r in g["rules"]}

    def test_rule_identity(self):
        self.assertEqual(self.rule["apiVersion"], "monitoring.coreos.com/v1")
        self.assertEqual(self.rule["metadata"]["namespace"], "mias")
        labels = self.rule["metadata"]["labels"]
        self.assertEqual((labels["app.kubernetes.io/name"], labels["app.kubernetes.io/part-of"]), ("mias-alerts", "mias"))
        # Evaluated by the UWM Thanos Ruler (platform kube-state-metrics needed), never leaf-prometheus only.
        self.assertNotIn("openshift.io/prometheus-rule-evaluation-scope", labels)
        self.assertEqual([g["name"] for g in self.rule["spec"]["groups"]],
                         ["mias.availability", "mias.artifacts", "mias.telemetry"])

    def test_exact_alert_set_without_synthetic(self):
        self.assertEqual(set(self.alerts), set(EXPECTED))
        self.assertNotIn("MiasUiSyntheticFailing", self.text)
        self.assertNotIn("probe_success", self.text)

    def test_severity_for_and_labels(self):
        for name, (severity, duration, component) in EXPECTED.items():
            rule = self.alerts[name]
            self.assertEqual(rule.get("for"), duration, name)
            self.assertEqual(rule["labels"], {"severity": severity, "component": component, "part_of": "mias"}, name)
            ann = rule["annotations"]
            self.assertEqual(set(ann), {"summary", "description", "runbook_hint"}, name)
            for value in ann.values():
                self.assertTrue(value and len(value) < 300, name)
                self.assertNotRegex(value, r"https?://|token|Bearer|password", name)

    def test_expressions_use_only_verified_metrics_scoped_to_the_namespace(self):
        for name, rule in self.alerts.items():
            expr = rule["expr"]
            used = metric_names(expr)
            self.assertTrue(used, name)
            self.assertLessEqual(used, ALLOWED_METRICS, name)
            for selector in re.findall(r"\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", expr):
                if "=" in selector:
                    self.assertIn('namespace="mias"', selector, name)

    def test_specific_expression_semantics(self):
        five_xx = self.alerts["MiasApi5xxRatio"]["expr"]
        self.assertIn('http_route!~"/health/.*"', five_xx)
        self.assertIn('http_route="/api/v1/alerts/{artifact_id}/deliveries",http_response_status_code="503"', five_xx)
        self.assertIn("[10m]", five_xx)
        self.assertIn("> 0.05", five_xx)
        self.assertIn("or vector(0)", five_xx)
        self.assertIn('exported_job="mias/mias-api"', self.alerts["MiasArtifactIndexUnhealthy"]["expr"])
        self.assertIn("> 300", self.alerts["MiasArtifactIndexStale"]["expr"])
        self.assertIn("> 0.80", self.alerts["MiasArtifactPvcFilling"]["expr"])
        self.assertIn('persistentvolumeclaim="mias-artifacts"', self.alerts["MiasArtifactPvcFilling"]["expr"])
        self.assertIn("[30m]) > 2", self.alerts["MiasPodRestarting"]["expr"])
        self.assertIn('reason="CrashLoopBackOff"', self.alerts["MiasPodRestarting"]["expr"])
        self.assertIn("absent_over_time(", self.alerts["MiasTelemetryPipelineStalled"]["expr"])
        self.assertIn('job="otel-collector"', self.alerts["MiasCollectorDown"]["expr"])
        self.assertNotRegex(" ".join(r["expr"] for r in self.alerts.values()), r"otelcol_")   # self-metrics are off

    def test_ui_alert_follows_ui_enabled(self):
        objs, text = render("ui.enabled=false")
        rule = objs[("PrometheusRule", "mias-alerts")]
        names = {r["alert"] for g in rule["spec"]["groups"] for r in g["rules"]}
        self.assertEqual(names, set(EXPECTED) - {"MiasUiUnavailable"})
        self.assertNotIn("mias-ui", text)

    def test_additive_only(self):
        off, off_text = render("monitoring.alerts.enabled=false")
        self.assertNotIn(("PrometheusRule", "mias-alerts"), off)
        self.assertEqual(set(self.objs) - set(off), {("PrometheusRule", "mias-alerts")})
        for key, obj in off.items():
            self.assertEqual(self.objs[key], obj, key)          # pod templates, Routes, policies, ConfigMaps identical
        for banned in ("kind: Secret", "kind: Role", "kind: RoleBinding", "kind: ClusterRole"):
            self.assertNotIn(banned, self.text)
        self.assertIn("helm.sh/chart: mias-0.3.1", self.text)


if __name__ == "__main__":
    unittest.main()
