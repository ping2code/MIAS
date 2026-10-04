"""Phase 16B/16F: the mias-ui part of the mias Helm chart (ui.*). Renders with the local `helm` binary (skipped if
absent); no cluster access. Since 16F the UI is enabled by default with the deployed digest pinned; with
ui.enabled=false (and alerts off) the chart must render the Phase 15 objects, apart from the derived and intentionally
added values listed at PHASE15_OBJECTS."""
import hashlib
import json
import re
import subprocess
import unittest

from tests.test_helm_mias import (CHART, DIGEST, HELM, OAUTH_OFF, PRE_TASK8, ROUTE_TLS_RBAC, SYNTHETIC_OBJECTS, UI_OAUTH_OBJECTS,
                                  render, route_tls)
from tests.test_openshift_manifests import parse, walk

UI_DIGEST = "sha256:" + "ab" * 32                     # a syntactically valid placeholder for render-only tests
# Phase 16F: the deployed mias-ui image (merged main 90effc1778a7612e8a708194644b46b79db09641), pinned in values.yaml.
DEPLOYED_UI_DIGEST = "sha256:415e38e1921dd99540df79d16e7db2e8b3cc7d8638303ccb569eb0424d5f0640"   # Task 8 Stage 3
# The pre-Task 8 UI shape (no oauth-proxy); the OAuth layer is tested in tests/test_helm_auth.py.
UI_ON = ("ui.enabled=true", f"ui.image.digest={UI_DIGEST}", OAUTH_OFF, "ui.api.injectToken=false")
UI_OBJECTS = {("ServiceAccount", "mias-ui"), ("ConfigMap", "mias-ui-config"), ("Deployment", "mias-ui"),
              ("Service", "mias-ui"), ("Route", "mias-ui"), ("NetworkPolicy", "mias-ui-allow-router"),
              ("NetworkPolicy", "mias-ui-egress-api"), ("NetworkPolicy", "mias-api-allow-ui"),
              ("PodDisruptionBudget", "mias-ui")}                  # Hardening Task 5 (tests/test_helm_ui_ha.py)
# Phase 15 (origin/main 30455ea) rendered per value set, parsed, and hashed as canonical JSON with three derived or
# intentionally added values removed: each pod template's checksum/config (an input hash whose algorithm changed in
# Hardening Task 2), the helm.sh/chart label, and the Routes' disable_cookies annotation (Hardening Task 2). Hardening
# Task 7's additions are removed too: the hsts_header annotation, the Route externalCertificate reference, and the
# router's Route-TLS Role/RoleBinding.
PHASE15_OBJECTS = {
    (): "6da0e3138c4887b6",
    ("observability.enabled=false",): "9ffd285162eb4e8a",
    ("networkPolicy.enabled=false",): "c8739f6ec3b3520d",
    ("route.enabled=false",): "2ed5cc465d4ff1b0",
}
ALERTS = ("PrometheusRule", "mias-alerts")
TLS_ROLE = ("Role", "mias-route-tls-reader")          # lists the UI certificate Secret only while the UI Route exists


def alert_names(objs):
    return {r["alert"] for g in objs[ALERTS]["spec"]["groups"] for r in g["rules"]}


DNS_RULE = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}},
                    "podSelector": {"matchLabels": {"dns.operator.openshift.io/daemonset-dns": "default"}}}],
            "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}]}


def raw_render(*sets):
    args = [HELM, "template", "mias", CHART, "--namespace", "mias"]
    for item in sets:
        args += ["--set", item]
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def canonical_objects_hash(*sets):
    """The parsed render as canonical JSON, without the derived/added values listed at PHASE15_OBJECTS."""
    out = raw_render(*sets)
    if out.returncode != 0:
        raise AssertionError(out.stderr[-500:])
    objs = []
    for chunk in re.split(r"^---\s*$", out.stdout, flags=re.M):
        if not any(line.strip() and not line.lstrip().startswith("#") for line in chunk.splitlines()):
            continue
        obj = parse(chunk)
        if (obj["kind"], obj["metadata"]["name"]) in ROUTE_TLS_RBAC:
            continue
        obj["metadata"].get("labels", {}).pop("helm.sh/chart", None)
        if obj["kind"] == "Route":
            obj["spec"]["tls"].pop("externalCertificate", None)
        annotations = obj["metadata"].get("annotations")
        if annotations is not None:
            annotations.pop("haproxy.router.openshift.io/disable_cookies", None)
            annotations.pop("haproxy.router.openshift.io/hsts_header", None)
            if not annotations:
                del obj["metadata"]["annotations"]
        template = obj.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations")
        if template:
            template.pop("checksum/config", None)
        objs.append(obj)
    objs.sort(key=lambda o: (o["kind"], o["metadata"]["name"]))
    return hashlib.sha256(json.dumps(objs, sort_keys=True).encode()).hexdigest()[:16]


@unittest.skipUnless(HELM, "helm binary not available")
class HelmUiDisabledTests(unittest.TestCase):
    def test_disabled_render_equals_phase15(self):
        # With the UI and alerts off, every object equals Phase 15 except the derived/added values (see above).
        for sets, expected in PHASE15_OBJECTS.items():
            self.assertEqual(canonical_objects_hash(*sets, "ui.enabled=false", "monitoring.alerts.enabled=false"),
                             expected, sets)

    def test_default_deploys_the_pinned_ui_image(self):
        objs, _ = render()
        self.assertTrue(UI_OBJECTS <= set(objs))
        dep = objs[("Deployment", "mias-ui")]
        self.assertEqual(dep["spec"]["template"]["spec"]["containers"][0]["image"],
                         f"image-registry.openshift-image-registry.svc:5000/mias/mias-ui@{DEPLOYED_UI_DIGEST}")
        self.assertEqual(dep["metadata"]["labels"]["app.kubernetes.io/version"], "ad5c2e1e664c")
        disabled, _ = render("ui.enabled=false")
        # Rollback removes exactly the UI objects and the synthetic probe of the UI Route (alerts stay)
        self.assertEqual(set(objs) - set(disabled), UI_OBJECTS | UI_OAUTH_OBJECTS | SYNTHETIC_OBJECTS)
        for key, obj in disabled.items():
            if key in (ALERTS, TLS_ROLE, ("NetworkPolicy", "mias-ui-allow-router")):
                continue                                            # the UI alerts and UI cert grant follow ui.enabled
            self.assertEqual(objs[key], obj, key)                   # and leaves every other object identical
        self.assertEqual(alert_names(objs) - alert_names(disabled),
                         {"MiasUiUnavailable", "MiasUiSyntheticFailing", "MiasTlsCertificateExpiring"})

    def test_disabled_renders_no_ui_object(self):
        objs, text = render("ui.enabled=false")
        self.assertFalse(UI_OBJECTS & set(objs))
        self.assertNotIn("mias-ui", text)

    def test_enabled_requires_a_pinned_digest(self):
        for bad in ("", "latest", "sha256:abc", "mias-ui:1.0"):
            with self.assertRaises(AssertionError):
                render("ui.enabled=true", f"ui.image.digest={bad}")

    def test_upstream_must_be_host_port(self):
        for bad in ("http://mias-api:8080", "mias-api", "mias-api:8080/x", "MIAS-API:8080"):
            with self.assertRaises(AssertionError):
                render(*UI_ON, f"ui.api.upstream={bad}")


@unittest.skipUnless(HELM, "helm binary not available")
class HelmUiEnabledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render(*UI_ON)
        cls.dep = cls.objs[("Deployment", "mias-ui")]
        cls.pod = cls.dep["spec"]["template"]["spec"]
        cls.c = cls.pod["containers"][0]

    def test_objects_and_phase15_untouched(self):
        self.assertTrue(UI_OBJECTS <= set(self.objs))
        base, _ = render("ui.enabled=false")
        self.assertEqual(set(self.objs) - set(base), UI_OBJECTS | SYNTHETIC_OBJECTS)
        for key, obj in base.items():
            if key in (ALERTS, TLS_ROLE):
                continue                                          # the UI alerts and UI cert grant follow ui.enabled
            self.assertEqual(self.objs[key], obj, key)            # every Phase 15 object is byte-for-byte the same
        self.assertTrue(self.objs[("Deployment", "mias-api")]["spec"]["template"]["spec"]["containers"][0]["image"]
                        .endswith(DIGEST))

    def test_no_secret_rbac_or_hpa(self):
        kinds = {k for k, _ in self.objs}
        for banned in ("Secret", "ClusterRole", "ClusterRoleBinding", "HorizontalPodAutoscaler"):
            self.assertNotIn(banned, kinds)
        self.assertEqual({k for k in self.objs if k[0] in ("Role", "RoleBinding")}, ROUTE_TLS_RBAC)
        ui_text = subprocess.run([HELM, "template", "mias", CHART, "--namespace", "mias", "--show-only",
                                  "templates/ui.yaml"] + [a for s in UI_ON for a in ("--set", s)],
                                 capture_output=True, text=True, timeout=60).stdout
        for banned in ("secretKeyRef", "MIAS_API_READ_TOKEN", "Authorization", "Bearer", "token:", "stringData"):
            self.assertNotIn(banned, ui_text)
        self.assertEqual(self.objs[("ConfigMap", "mias-ui-config")]["data"],
                         {"MIAS_UI_API_UPSTREAM": "mias-api.mias.svc.cluster.local:8080"})

    def test_workload_security(self):
        self.assertEqual(self.c["image"],
                         f"image-registry.openshift-image-registry.svc:5000/mias/mias-ui@{UI_DIGEST}")
        self.assertEqual((self.dep["spec"]["replicas"], self.pod["serviceAccountName"],
                          self.pod["automountServiceAccountToken"], self.pod["enableServiceLinks"]),
                         (2, "mias-ui", False, False))   # Hardening Task 5: two replicas
        self.assertEqual(self.objs[("ServiceAccount", "mias-ui")]["automountServiceAccountToken"], False)
        self.assertEqual(self.pod["securityContext"], {"runAsNonRoot": True, "fsGroupChangePolicy": "OnRootMismatch",
                                                       "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(self.c["securityContext"], {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})
        keys = {k for k, _ in walk(self.dep)}
        for banned in ("runAsUser", "runAsGroup", "fsGroup", "privileged", "hostNetwork", "hostPath", "hostPort",
                       "persistentVolumeClaim", "env"):
            self.assertNotIn(banned, keys)
        self.assertEqual(self.c["envFrom"], [{"configMapRef": {"name": "mias-ui-config"}}])
        self.assertEqual(self.c["volumeMounts"], [{"name": "tmp", "mountPath": "/tmp"}])
        self.assertEqual(self.pod["volumes"], [{"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}])
        self.assertEqual(self.c["resources"], {"requests": {"cpu": "10m", "memory": "32Mi"},
                                               "limits": {"cpu": "200m", "memory": "128Mi"}})
        self.assertEqual(self.dep["spec"]["strategy"],
                         {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}})
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            self.assertEqual(self.c[probe]["httpGet"], {"path": "/healthz", "port": "http"})
        self.assertEqual(self.c["ports"], [{"name": "http", "containerPort": 8080, "protocol": "TCP"}])
        self.assertEqual(self.dep["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/part-of"], "mias")
        self.assertRegex(self.dep["spec"]["template"]["metadata"]["annotations"]["checksum/config"], r"^[0-9a-f]{64}$")

    def test_service_and_route(self):
        svc = self.objs[("Service", "mias-ui")]["spec"]
        self.assertEqual((svc["type"], svc["selector"]), ("ClusterIP", {"app.kubernetes.io/name": "mias-ui"}))
        self.assertEqual(svc["ports"], [{"name": "http", "port": 8080, "targetPort": "http", "protocol": "TCP"}])
        route = self.objs[("Route", "mias-ui")]["spec"]
        self.assertEqual(route["host"], "mias-ui.apps.ngc.sirii.org")
        self.assertEqual(route["to"], {"kind": "Service", "name": "mias-ui", "weight": 100})
        self.assertEqual(route["tls"], route_tls("mias-ui-tls"))         # Hardening Task 7: MIAS lab certificate
        objs, _ = render(*UI_ON, "ui.route.enabled=false")
        self.assertNotIn(("Route", "mias-ui"), objs)
        self.assertIn(("Route", "mias-api"), objs)

    def test_network_policies_are_narrow(self):
        router = self.objs[("NetworkPolicy", "mias-ui-allow-router")]["spec"]
        self.assertEqual((router["podSelector"], router["policyTypes"]),
                         ({"matchLabels": {"app.kubernetes.io/name": "mias-ui"}}, ["Ingress"]))
        self.assertEqual(router["ingress"], [
            {"from": [{"namespaceSelector": {"matchLabels": {"policy-group.network.openshift.io/ingress": ""}}}],
             "ports": [{"protocol": "TCP", "port": 8080}]}])
        egress = self.objs[("NetworkPolicy", "mias-ui-egress-api")]["spec"]
        self.assertEqual((egress["podSelector"], egress["policyTypes"]),
                         ({"matchLabels": {"app.kubernetes.io/name": "mias-ui"}}, ["Egress"]))
        self.assertEqual(egress["egress"], [
            {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "mias-api"}}}],
             "ports": [{"protocol": "TCP", "port": 8080}]}, DNS_RULE])
        api = self.objs[("NetworkPolicy", "mias-api-allow-ui")]["spec"]
        self.assertEqual((api["podSelector"], api["policyTypes"]),
                         ({"matchLabels": {"app.kubernetes.io/name": "mias-api"}}, ["Ingress"]))
        self.assertEqual(api["ingress"], [
            {"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "mias-ui"}}}],
             "ports": [{"protocol": "TCP", "port": 8080}]}])
        # The existing router rule and default deny are unchanged; no ipBlock or wildcard anywhere.
        self.assertIn(("NetworkPolicy", "mias-default-deny"), self.objs)
        mias_policies = [o for k, o in self.objs.items() if k[0] == "NetworkPolicy" and o["metadata"]["namespace"] == "mias"]
        self.assertNotIn("ipBlock", repr(mias_policies))      # the synthetic exporter's /32 is in its own namespace
        self.assertNotIn("0.0.0.0/0", self.text)
        objs, _ = render(*UI_ON, "networkPolicy.enabled=false")
        self.assertFalse({k for k in objs if k[0] == "NetworkPolicy"})
        self.assertIn(("Deployment", "mias-ui"), objs)

    def test_upstream_override_and_version_label(self):
        objs, _ = render(*UI_ON, "ui.api.upstream=mias-api.other.svc.cluster.local:8080",
                         "ui.image.versionLabel=4398d07e5319")
        self.assertEqual(objs[("ConfigMap", "mias-ui-config")]["data"]["MIAS_UI_API_UPSTREAM"],
                         "mias-api.other.svc.cluster.local:8080")
        self.assertEqual(objs[("Deployment", "mias-ui")]["metadata"]["labels"]["app.kubernetes.io/version"],
                         "4398d07e5319")
        objs, _ = render(*UI_ON, "ui.api.clusterDomain=example.internal")
        self.assertEqual(objs[("ConfigMap", "mias-ui-config")]["data"]["MIAS_UI_API_UPSTREAM"],
                         "mias-api.mias.svc.example.internal:8080")


if __name__ == "__main__":
    unittest.main()
