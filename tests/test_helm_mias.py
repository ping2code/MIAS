"""Phase 14E: the mias Helm chart (deploy/helm/mias) renders the locked, hardened deployment.

Renders with the local `helm` binary (skipped if absent) and parses the output with the strict block-YAML parser
from the manifest tests. No cluster access.
"""
import os
import re
import shutil
import subprocess
import unittest

from tests.test_openshift_manifests import IMAGE, parse, walk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "deploy", "helm", "mias")
RAW = os.path.join(ROOT, "deploy", "openshift")
DIGEST = "sha256:f915fb6c335330962ea2371c890b4cc6adb8962732e035e1b7d3c1b61c10e73a"
HELM = shutil.which("helm")


def render(*sets):
    args = [HELM, "template", "mias", CHART, "--namespace", "mias"]
    for item in sets:
        args += ["--set", item]
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-500:])
    chunks = re.split(r"^---\s*$", result.stdout, flags=re.M)
    docs = [parse(chunk) for chunk in chunks
            if any(not line.lstrip().startswith("#") for line in chunk.splitlines() if line.strip())]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}, result.stdout


def chart_text():
    text = []
    for directory, _, files in os.walk(CHART):
        for name in files:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                text.append(handle.read())
    return "\n".join(text)


@unittest.skipUnless(HELM, "helm binary not available")
class HelmChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render()
        cls.api = cls.objs[("Deployment", "mias-api")]
        cls.pub = cls.objs[("Deployment", "mias-publisher")]
        cls.api_pod, cls.pub_pod = cls.api["spec"]["template"]["spec"], cls.pub["spec"]["template"]["spec"]
        cls.api_c, cls.pub_c = cls.api_pod["containers"][0], cls.pub_pod["containers"][0]

    def test_lint_and_metadata(self):
        result = subprocess.run([HELM, "lint", CHART, "--namespace", "mias"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        chart = parse(open(os.path.join(CHART, "Chart.yaml"), encoding="utf-8").read())
        self.assertEqual((chart["apiVersion"], chart["name"], chart["type"]), ("v2", "mias", "application"))

    def test_exact_object_set(self):
        self.assertEqual(sorted(self.objs), sorted([
            ("ServiceAccount", "mias-api"), ("ServiceAccount", "mias-publisher"), ("ConfigMap", "mias-api-config"),
            ("ConfigMap", "mias-publisher-config"), ("PersistentVolumeClaim", "mias-artifacts"),
            ("Deployment", "mias-api"), ("Deployment", "mias-publisher"), ("Service", "mias-api"),
            ("Route", "mias-api"), ("NetworkPolicy", "mias-default-deny"), ("NetworkPolicy", "mias-api-allow-router")]))
        for banned in ("Secret", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding", "HorizontalPodAutoscaler",
                       "SecurityContextConstraints", "Job", "CronJob", "StatefulSet"):
            self.assertNotIn(banned, {k for k, _ in self.objs})
        self.assertTrue(all(o["metadata"]["namespace"] == "mias" for o in self.objs.values()))

    def test_digest_pinning_same_image(self):
        self.assertEqual(self.api_c["image"], f"image-registry.openshift-image-registry.svc:5000/mias/mias-api@{DIGEST}")
        self.assertEqual(self.pub_c["image"], self.api_c["image"])
        self.assertRegex(self.api_c["image"], IMAGE)
        raw_api = parse(open(os.path.join(RAW, "base", "deployment.yaml"), encoding="utf-8").read())
        self.assertEqual(raw_api["spec"]["template"]["spec"]["containers"][0]["image"], self.api_c["image"])
        with self.assertRaises(AssertionError):                                  # a tag or bad digest is refused
            render("image.digest=latest")
        self.assertNotIn(":latest", self.text)

    def test_replicas_strategy_and_affinity(self):
        self.assertEqual((self.api["spec"]["replicas"], self.api["spec"]["strategy"]["type"]), (1, "Recreate"))
        self.assertEqual((self.pub["spec"]["replicas"], self.pub_c["command"]), (0, ["sleep", "infinity"]))
        self.assertEqual(self.pub_pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"],
                         [{"labelSelector": {"matchLabels": {"app.kubernetes.io/name": "mias-api"}},
                           "topologyKey": "kubernetes.io/hostname"}])
        self.assertEqual(self.api["spec"]["selector"], {"matchLabels": {"app.kubernetes.io/name": "mias-api"}})
        self.assertEqual(self.pub["spec"]["selector"], {"matchLabels": {"app.kubernetes.io/name": "mias-publisher"}})

    def test_security(self):
        for pod, container in ((self.api_pod, self.api_c), (self.pub_pod, self.pub_c)):
            self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "fsGroupChangePolicy": "OnRootMismatch",
                                                      "seccompProfile": {"type": "RuntimeDefault"}})
            self.assertEqual(container["securityContext"], {
                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})
            self.assertEqual(pod["automountServiceAccountToken"], False)
        keys = {k for o in self.objs.values() for k, _ in walk(o)}
        for banned in ("runAsUser", "runAsGroup", "fsGroup", "privileged", "hostNetwork", "hostPID", "hostIPC",
                       "hostPath", "add", "nodeName", "nodeSelector"):
            self.assertNotIn(banned, keys)
        for sa in ("mias-api", "mias-publisher"):
            self.assertEqual(self.objs[("ServiceAccount", sa)]["automountServiceAccountToken"], False)

    def test_mounts_and_storage(self):
        api_mounts = {m["name"]: m for m in self.api_c["volumeMounts"]}
        pub_mounts = {m["name"]: m for m in self.pub_c["volumeMounts"]}
        self.assertEqual(api_mounts["artifacts"]["readOnly"], True)
        self.assertEqual(pub_mounts["artifacts"]["readOnly"], False)
        for pod in (self.api_pod, self.pub_pod):
            volumes = {v["name"]: v for v in pod["volumes"]}
            self.assertEqual(volumes["artifacts"]["persistentVolumeClaim"], {"claimName": "mias-artifacts"})
            self.assertEqual(volumes["tmp"]["emptyDir"], {"sizeLimit": "64Mi"})
        pvc = self.objs[("PersistentVolumeClaim", "mias-artifacts")]
        self.assertEqual(pvc["metadata"]["annotations"], {"helm.sh/resource-policy": "keep"})
        self.assertEqual((pvc["spec"]["accessModes"], pvc["spec"]["storageClassName"],
                          pvc["spec"]["resources"]["requests"]["storage"]), (["ReadWriteOnce"], "thin-csi", "1Gi"))
        objs, _ = render("persistence.enabled=false")
        self.assertNotIn(("PersistentVolumeClaim", "mias-artifacts"), objs)   # existing claim, still referenced
        self.assertEqual(objs[("Deployment", "mias-api")]["spec"]["template"]["spec"]["volumes"][0]
                         ["persistentVolumeClaim"], {"claimName": "mias-artifacts"})

    def test_configuration_and_secret(self):
        self.assertEqual(self.objs[("ConfigMap", "mias-api-config")]["data"], {
            "MIAS_API_HOST": "0.0.0.0", "MIAS_API_PORT": "8080", "MIAS_API_DOCS_ENABLED": "false",
            "MIAS_ARTIFACT_ROOT": "/var/lib/mias/artifacts"})
        self.assertEqual(self.objs[("ConfigMap", "mias-publisher-config")]["data"],
                         {"MIAS_ARTIFACT_ROOT": "/var/lib/mias/artifacts"})
        self.assertEqual(self.api_c["env"], [{"name": "MIAS_API_READ_TOKEN", "valueFrom": {
            "secretKeyRef": {"name": "mias-api-auth", "key": "MIAS_API_READ_TOKEN"}}}])
        self.assertNotIn("env", self.pub_c)
        self.assertEqual(self.pub_c["envFrom"], [{"configMapRef": {"name": "mias-publisher-config"}}])
        text = chart_text()
        for banned in ("stringData", "kind: Secret", "MIAS_RECEIPT_ROOT", "MIAS_API_OPERATOR_TOKEN",
                       "MIAS_API_ACTIONS_ENABLED", "REDIS", "DATABASE_URL", "POSTGRES", "TELEGRAM", "OPENAI", "MASSIVE"):
            self.assertNotIn(banned, text)
        self.assertNotRegex(text, r"(?i)token:\s*\S{20,}")                     # no inline token values

    def test_probes_and_resources(self):
        self.assertEqual(self.api_c["startupProbe"]["httpGet"], {"path": "/health/live", "port": "http"})
        self.assertEqual(self.api_c["livenessProbe"]["httpGet"], {"path": "/health/live", "port": "http"})
        self.assertEqual(self.api_c["readinessProbe"]["httpGet"], {"path": "/health/ready", "port": "http"})
        self.assertEqual(self.api_c["resources"], {"requests": {"cpu": "50m", "memory": "128Mi"},
                                                   "limits": {"cpu": "500m", "memory": "512Mi"}})
        self.assertEqual(self.pub_c["resources"], {"requests": {"cpu": "10m", "memory": "64Mi"},
                                                   "limits": {"cpu": "500m", "memory": "512Mi"}})

    def test_service_and_route(self):
        svc = self.objs[("Service", "mias-api")]["spec"]
        self.assertEqual((svc["type"], svc["ports"]), ("ClusterIP", [{"name": "http", "port": 8080,
                                                                      "targetPort": "http", "protocol": "TCP"}]))
        route = self.objs[("Route", "mias-api")]
        self.assertEqual(route["spec"]["host"], "mias-api.apps.ngc.sirii.org")
        self.assertEqual(route["spec"]["tls"], {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"})
        self.assertNotIn("annotations", route["metadata"])                       # router defaults retained

    def test_network_policies(self):
        deny = self.objs[("NetworkPolicy", "mias-default-deny")]["spec"]
        self.assertEqual(deny, {"podSelector": {"matchLabels": {"app.kubernetes.io/part-of": "mias"}},
                                "policyTypes": ["Ingress", "Egress"]})
        allow = self.objs[("NetworkPolicy", "mias-api-allow-router")]["spec"]
        self.assertEqual(allow["podSelector"], {"matchLabels": {"app.kubernetes.io/name": "mias-api"}})
        self.assertEqual(allow["policyTypes"], ["Ingress"])
        self.assertEqual(allow["ingress"], [{"from": [{"namespaceSelector": {"matchLabels": {
            "policy-group.network.openshift.io/ingress": ""}}}], "ports": [{"protocol": "TCP", "port": 8080}]}])
        self.assertNotIn("0.0.0.0/0", self.text)
        self.assertNotIn("ipBlock", self.text)
        for pod in (self.api, self.pub):                                         # both selected by the default deny
            self.assertEqual(pod["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/part-of"], "mias")
        objs, _ = render("networkPolicy.enabled=false")
        self.assertFalse([k for k in objs if k[0] == "NetworkPolicy"])

    def test_adoption_render_matches_raw_baseline(self):
        """With the 14E hardening switched off, the chart reproduces the raw 14C/14D manifests' specs."""
        objs, _ = render("podSecurity.fsGroupChangePolicy=", "networkPolicy.enabled=false")
        pairs = {("Deployment", "mias-api"): "base/deployment.yaml", ("Service", "mias-api"): "base/service.yaml",
                 ("Route", "mias-api"): "base/route.yaml", ("ConfigMap", "mias-api-config"): "base/configmap.yaml",
                 ("PersistentVolumeClaim", "mias-artifacts"): "base/pvc.yaml",
                 ("Deployment", "mias-publisher"): "publisher/deployment.yaml",
                 ("ConfigMap", "mias-publisher-config"): "publisher/configmap.yaml"}
        for key, path in pairs.items():
            with self.subTest(object=key):
                raw = parse(open(os.path.join(RAW, path), encoding="utf-8").read())
                field = "data" if key[0] == "ConfigMap" else "spec"
                self.assertEqual(objs[key][field], raw[field])


if __name__ == "__main__":
    unittest.main()
