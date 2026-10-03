"""Phase 16B: the mias-ui part of the mias Helm chart (ui.*). Renders with the local `helm` binary (skipped if
absent); no cluster access. With ui.enabled=false (the default) the chart must render exactly the Phase 15 objects."""
import hashlib
import subprocess
import unittest

from tests.test_helm_mias import CHART, DIGEST, HELM, render
from tests.test_openshift_manifests import walk

UI_DIGEST = "sha256:" + "ab" * 32                     # a syntactically valid placeholder; no UI image is pushed in 16B
UI_ON = ("ui.enabled=true", f"ui.image.digest={UI_DIGEST}")
UI_OBJECTS = {("ServiceAccount", "mias-ui"), ("ConfigMap", "mias-ui-config"), ("Deployment", "mias-ui"),
              ("Service", "mias-ui"), ("Route", "mias-ui"), ("NetworkPolicy", "mias-ui-allow-router"),
              ("NetworkPolicy", "mias-ui-egress-api"), ("NetworkPolicy", "mias-api-allow-ui")}
# sha256 of `helm template mias deploy/helm/mias --namespace mias` at Phase 15 (origin/main 30455ea), per value set.
PHASE15_RENDER = {
    (): "5ee344113044501b",
    ("observability.enabled=false",): "0e57da8caf13eeaf",
    ("networkPolicy.enabled=false",): "91c7caf038cd1f59",
    ("route.enabled=false",): "947b64a95a4e94d3",
}
DNS_RULE = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}},
                    "podSelector": {"matchLabels": {"dns.operator.openshift.io/daemonset-dns": "default"}}}],
            "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}]}


def raw_render(*sets):
    args = [HELM, "template", "mias", CHART, "--namespace", "mias"]
    for item in sets:
        args += ["--set", item]
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


@unittest.skipUnless(HELM, "helm binary not available")
class HelmUiDisabledTests(unittest.TestCase):
    def test_disabled_render_equals_phase15(self):
        for sets, prefix in PHASE15_RENDER.items():
            for extra in ((), ("ui.enabled=false",)):
                out = raw_render(*sets, *extra)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(hashlib.sha256(out.stdout.encode()).hexdigest()[:16], prefix, (sets, extra))

    def test_disabled_renders_no_ui_object(self):
        objs, text = render()
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
        base, _ = render()
        self.assertEqual(set(self.objs) - set(base), UI_OBJECTS)
        for key, obj in base.items():
            self.assertEqual(self.objs[key], obj, key)            # every Phase 15 object is byte-for-byte the same
        self.assertTrue(self.objs[("Deployment", "mias-api")]["spec"]["template"]["spec"]["containers"][0]["image"]
                        .endswith(DIGEST))

    def test_no_secret_rbac_or_hpa(self):
        kinds = {k for k, _ in self.objs}
        for banned in ("Secret", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding", "HorizontalPodAutoscaler"):
            self.assertNotIn(banned, kinds)
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
                         (1, "mias-ui", False, False))
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
        self.assertEqual(route["tls"], {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"})
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
        self.assertNotIn("ipBlock", self.text)
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
