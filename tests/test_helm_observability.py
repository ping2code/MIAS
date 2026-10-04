"""Phase 15: the observability part of the mias Helm chart (OTEL configuration, collector, ServiceMonitor, telemetry
NetworkPolicies). Renders with the local `helm` binary (skipped if absent); no cluster access."""
import unittest

from tests.test_helm_mias import DIGEST, HELM, render
from tests.test_openshift_manifests import walk

COLLECTOR_DIGEST = "sha256:2d75615700fdecd6981f7b352f155f3f4db74d884d73f5d92049ad4b69b41aef"
OBS_OBJECTS = {("ServiceAccount", "otel-collector"), ("ConfigMap", "otel-collector-config"),
               ("Deployment", "otel-collector"), ("Service", "otel-collector"), ("ServiceMonitor", "otel-collector"),
               ("NetworkPolicy", "mias-api-egress-telemetry"), ("NetworkPolicy", "otel-collector-ingress")}


@unittest.skipUnless(HELM, "helm binary not available")
class HelmObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render()
        cls.collector = cls.objs[("Deployment", "otel-collector")]
        cls.cpod = cls.collector["spec"]["template"]["spec"]
        cls.ccont = cls.cpod["containers"][0]

    def test_api_otel_environment(self):
        data = self.objs[("ConfigMap", "mias-api-config")]["data"]
        self.assertEqual({k: v for k, v in data.items() if k.startswith(("OTEL_", "MIAS_OBSERVABILITY", "MIAS_LOG"))}, {
            "MIAS_OBSERVABILITY_ENABLED": "true", "MIAS_LOG_FORMAT": "json", "OTEL_SERVICE_NAME": "mias-api",
            "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=lab,k8s.namespace.name=mias",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector.mias.svc:4318",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "none", "OTEL_METRIC_EXPORT_INTERVAL": "60000"})
        api = self.objs[("Deployment", "mias-api")]
        self.assertRegex(api["spec"]["template"]["metadata"]["annotations"]["checksum/config"], r"^[0-9a-f]{64}$")
        self.assertTrue(api["spec"]["template"]["spec"]["containers"][0]["image"].endswith(DIGEST))
        pub = self.objs[("Deployment", "mias-publisher")]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(pub["envFrom"], [{"configMapRef": {"name": "mias-publisher-config"}}])   # publisher untouched

    def test_collector_workload_security(self):
        self.assertEqual(self.ccont["image"],
                         f"image-registry.openshift-image-registry.svc:5000/mias/otel-collector-contrib@{COLLECTOR_DIGEST}")
        self.assertEqual((self.collector["spec"]["replicas"], self.cpod["serviceAccountName"],
                          self.cpod["automountServiceAccountToken"]), (1, "otel-collector", False))
        self.assertEqual(self.objs[("ServiceAccount", "otel-collector")]["automountServiceAccountToken"], False)
        self.assertEqual(self.cpod["securityContext"], {"runAsNonRoot": True, "fsGroupChangePolicy": "OnRootMismatch",
                                                        "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(self.ccont["securityContext"], {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(self.ccont["resources"], {"requests": {"cpu": "20m", "memory": "64Mi"},
                                                   "limits": {"cpu": "300m", "memory": "256Mi"}})
        self.assertEqual({p["name"]: p["containerPort"] for p in self.ccont["ports"]},
                         {"otlp-http": 4318, "metrics": 8889, "health": 13133})
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            self.assertEqual(self.ccont[probe]["httpGet"], {"path": "/", "port": "health"})
        keys = {k for o in self.objs.values() for k, _ in walk(o)}
        for banned in ("runAsUser", "fsGroup", "privileged", "hostNetwork", "hostPath", "hostPort"):
            self.assertNotIn(banned, keys)
        svc = self.objs[("Service", "otel-collector")]["spec"]
        self.assertEqual(svc["type"], "ClusterIP")
        self.assertEqual({p["name"]: p["port"] for p in svc["ports"]}, {"otlp-http": 4318, "metrics": 8889})
        self.assertNotIn(("Route", "otel-collector"), self.objs)

    def test_collector_config(self):
        config = self.objs[("ConfigMap", "otel-collector-config")]["data"]["config.yaml"]
        for required in ("endpoint: 0.0.0.0:4318", "endpoint: 0.0.0.0:8889", "endpoint: 0.0.0.0:13133",
                         "verbosity: basic", "memory_limiter", "encoding: json", "level: none"):
            self.assertIn(required, config)
        self.assertNotIn("grpc", config)                       # only OTLP/HTTP is received
        self.assertNotIn("4317", config)
        self.assertNotIn("detailed", config)                   # no noisy debug output in the deployed state

    def test_service_monitor(self):
        monitor = self.objs[("ServiceMonitor", "otel-collector")]
        self.assertEqual(monitor["apiVersion"], "monitoring.coreos.com/v1")
        self.assertEqual(monitor["spec"], {"selector": {"matchLabels": {"app.kubernetes.io/name": "otel-collector"}},
                                           "endpoints": [{"port": "metrics", "interval": "30s", "scheme": "http",
                                                          "path": "/metrics"}]})

    def test_telemetry_network_policies_are_narrow(self):
        egress = self.objs[("NetworkPolicy", "mias-api-egress-telemetry")]["spec"]
        self.assertEqual((egress["podSelector"], egress["policyTypes"]),
                         ({"matchLabels": {"app.kubernetes.io/name": "mias-api"}}, ["Egress"]))
        self.assertEqual(egress["egress"], [
            {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "otel-collector"}}}],
             "ports": [{"protocol": "TCP", "port": 4318}]},
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}},
                     "podSelector": {"matchLabels": {"dns.operator.openshift.io/daemonset-dns": "default"}}}],
             "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}]}])
        ingress = self.objs[("NetworkPolicy", "otel-collector-ingress")]["spec"]
        self.assertEqual((ingress["podSelector"], ingress["policyTypes"]),
                         ({"matchLabels": {"app.kubernetes.io/name": "otel-collector"}}, ["Ingress"]))
        self.assertEqual(ingress["ingress"], [
            {"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "mias-api"}}}],
             "ports": [{"protocol": "TCP", "port": 4318}]},
            {"from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": "openshift-user-workload-monitoring"}},
                       "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}}}],
             "ports": [{"protocol": "TCP", "port": 8889}]}])
        # The default deny still covers every MIAS pod (the collector carries part-of: mias); no publisher egress.
        self.assertEqual(self.collector["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/part-of"], "mias")
        policies = [o for k, o in self.objs.items() if k[0] == "NetworkPolicy" and o["metadata"]["namespace"] == "mias"]
        self.assertNotIn("ipBlock", repr(policies))          # the synthetic exporter's /32 is in its own namespace
        self.assertNotIn("0.0.0.0/0", self.text)
        for policy in policies:
            selector = policy["spec"]["podSelector"].get("matchLabels", {})
            self.assertNotEqual(selector.get("app.kubernetes.io/name"), "mias-publisher")

    def test_disabled_and_partial_rendering(self):
        objs, text = render("observability.enabled=false")
        self.assertFalse(OBS_OBJECTS & set(objs))
        self.assertFalse([k for k in objs[("ConfigMap", "mias-api-config")]["data"]
                          if k.startswith(("OTEL_", "MIAS_OBSERVABILITY", "MIAS_LOG"))])
        objs, _ = render("observability.collector.enabled=false",
                         "observability.otlp.endpoint=http://otel.example.internal:4318")
        self.assertFalse({k for k in OBS_OBJECTS if k[1] != "mias-api-egress-telemetry"} & set(objs))
        self.assertEqual(objs[("ConfigMap", "mias-api-config")]["data"]["OTEL_EXPORTER_OTLP_ENDPOINT"],
                         "http://otel.example.internal:4318")
        objs, _ = render("observability.collector.serviceMonitor.enabled=false")
        self.assertNotIn(("ServiceMonitor", "otel-collector"), objs)
        objs, _ = render("observability.traces.enabled=false")
        self.assertEqual(objs[("ConfigMap", "mias-api-config")]["data"]["OTEL_TRACES_EXPORTER"], "none")
        with self.assertRaises(AssertionError):
            render("observability.collector.image.digest=latest")

    def test_no_secrets(self):
        for banned in ("kind: Secret", "stringData", "password", "api_key", "Authorization", "Bearer"):
            self.assertNotIn(banned, self.text)


if __name__ == "__main__":
    unittest.main()
