"""Phase 14D: static invariants of the OpenShift publisher toolbox manifests (deploy/openshift/publisher).

Reuses the strict block-YAML parser from the baseline manifest tests. No cluster access.
"""
import os
import unittest

from tests.test_openshift_manifests import BASE as API_BASE, IMAGE, parse, walk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISHER = os.path.join(ROOT, "deploy", "openshift", "publisher")
FILES = ("serviceaccount.yaml", "configmap.yaml", "deployment.yaml")


def load(directory, name):
    with open(os.path.join(directory, name), encoding="utf-8") as handle:
        return parse(handle.read())


def content(name):
    with open(os.path.join(PUBLISHER, name), encoding="utf-8") as handle:
        return "".join(line for line in handle if not line.lstrip().startswith("#"))


class PublisherManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: load(PUBLISHER, name) for name in FILES}
        cls.deployment = cls.docs["deployment.yaml"]
        cls.pod = cls.deployment["spec"]["template"]["spec"]
        cls.container = cls.pod["containers"][0]
        cls.api = load(API_BASE, "deployment.yaml")

    def test_exact_resource_set(self):
        self.assertEqual(sorted(f for f in os.listdir(PUBLISHER) if f.endswith((".yaml", ".yml"))), sorted(FILES))
        self.assertEqual({n: (d["kind"], d["metadata"]["name"], d["metadata"]["namespace"]) for n, d in self.docs.items()},
                         {"serviceaccount.yaml": ("ServiceAccount", "mias-publisher", "mias"),
                          "configmap.yaml": ("ConfigMap", "mias-publisher-config", "mias"),
                          "deployment.yaml": ("Deployment", "mias-publisher", "mias")})
        for banned in ("Service", "Route", "Secret", "PersistentVolumeClaim", "Role", "RoleBinding", "ClusterRole",
                       "ClusterRoleBinding", "HorizontalPodAutoscaler", "NetworkPolicy", "Job", "CronJob"):
            self.assertNotIn(banned, {d["kind"] for d in self.docs.values()})

    def test_scaled_to_zero_and_inert(self):
        spec = self.deployment["spec"]
        self.assertEqual((spec["replicas"], spec["strategy"]["type"]), (0, "Recreate"))
        self.assertEqual(self.container["command"], ["sleep", "infinity"])
        self.assertNotIn("args", self.container)
        for absent in ("ports", "livenessProbe", "readinessProbe", "startupProbe"):
            self.assertNotIn(absent, self.container)

    def test_same_image_as_api(self):
        self.assertRegex(self.container["image"], IMAGE)
        self.assertEqual(self.container["image"], self.api["spec"]["template"]["spec"]["containers"][0]["image"])

    def test_identity_and_rbac(self):
        self.assertEqual((self.pod["serviceAccountName"], self.pod["automountServiceAccountToken"]),
                         ("mias-publisher", False))
        self.assertEqual(self.docs["serviceaccount.yaml"]["automountServiceAccountToken"], False)

    def test_restricted_security_context(self):
        self.assertEqual(self.pod["securityContext"], {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(self.container["securityContext"], {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})
        keys = {k for d in self.docs.values() for k, _ in walk(d)}
        for banned in ("runAsUser", "runAsGroup", "fsGroup", "privileged", "hostNetwork", "hostPID", "hostIPC",
                       "hostPath", "add", "nodeName", "nodeSelector"):
            self.assertNotIn(banned, keys)

    def test_same_node_affinity_on_stable_label(self):
        rules = self.pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(rules, [{"labelSelector": {"matchLabels": {"app.kubernetes.io/name": "mias-api"}},
                                  "topologyKey": "kubernetes.io/hostname"}])
        api_labels = self.api["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(api_labels["app.kubernetes.io/name"], "mias-api")       # the label the affinity relies on
        self.assertNotIn("ngc-282t4", "".join(content(f) for f in FILES))         # no hard-coded node

    def test_volumes(self):
        mounts = {m["name"]: m for m in self.container["volumeMounts"]}
        self.assertEqual(mounts["artifacts"], {"name": "artifacts", "mountPath": "/var/lib/mias/artifacts",
                                               "readOnly": False})
        self.assertEqual(mounts["tmp"], {"name": "tmp", "mountPath": "/tmp"})
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        self.assertEqual(volumes["artifacts"]["persistentVolumeClaim"], {"claimName": "mias-artifacts"})
        self.assertEqual(volumes["tmp"]["emptyDir"], {"sizeLimit": "64Mi"})
        # The API keeps its read-only container mount of the same claim.
        api_mounts = {m["name"]: m for m in self.api["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
        self.assertTrue(api_mounts["artifacts"]["readOnly"])

    def test_configuration_isolation(self):
        self.assertEqual(self.docs["configmap.yaml"]["data"], {"MIAS_ARTIFACT_ROOT": "/var/lib/mias/artifacts"})
        self.assertEqual(self.container["envFrom"], [{"configMapRef": {"name": "mias-publisher-config"}}])
        self.assertNotIn("env", self.container)
        text = "".join(content(f) for f in FILES)
        for banned in ("mias-api-auth", "mias-api-config", "secretKeyRef", "MIAS_API_READ_TOKEN", "MIAS_API_HOST",
                       "MIAS_API_PORT", "MIAS_RECEIPT_ROOT", "MIAS_API_OPERATOR_TOKEN", "MIAS_API_ACTIONS_ENABLED",
                       "REDIS", "DATABASE_URL", "POSTGRES", "TELEGRAM", "OPENAI", "MASSIVE", "OPTIONS_DATA",
                       "MARKET_DATA", "http://", "https://", ":latest"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
