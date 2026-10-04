"""Phase 14C: static invariants of the OpenShift baseline manifests (deploy/openshift/base). No cluster access.

The manifests use plain block YAML only (mappings, lists, scalars, comments, and `|` literal blocks for ConfigMap
file content). A small strict parser is included so no YAML dependency is added; anything outside that subset fails
the test rather than being misread.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "deploy", "openshift", "base")
FILES = ("serviceaccount.yaml", "configmap.yaml", "pvc.yaml", "deployment.yaml", "service.yaml", "route.yaml")
IMAGE = re.compile(r"image-registry\.openshift-image-registry\.svc:5000/mias/mias-api@sha256:[0-9a-f]{64}")


def scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text == "{}":                     # the empty map, e.g. a NetworkPolicy podSelector selecting every pod
        return {}
    if text.startswith(("{", "[", "&", "*", "|", ">", "!")):
        raise ValueError(f"unsupported YAML construct: {text!r}")
    return text


def parse(text):
    """Strict block-YAML subset: returns the document as dicts/lists/scalars."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if stripped.strip():
            if "\t" in raw:
                raise ValueError("tabs are not allowed")
            lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))

    def block(i, indent):
        if lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                item = lines[i][1][2:]
                if ":" in item and not item.startswith(("\"", "'")):
                    # a mapping that starts on the dash line: re-read it as a nested block at indent + 2
                    lines[i] = (indent + 2, item)
                    value, i = block(i, indent + 2)
                else:
                    value, i = scalar(item), i + 1
                out.append(value)
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
            key, _, rest = lines[i][1].partition(":")
            if not _:
                raise ValueError(f"expected a mapping entry: {lines[i][1]!r}")
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            if rest.strip() in ("|", "|-"):
                # literal block scalar (ConfigMap file content): the more-indented lines, re-indented relatively
                j = i + 1
                while j < len(lines) and lines[j][0] > indent:
                    j += 1
                body = lines[i + 1:j]
                base = body[0][0] if body else 0
                out[key] = "\n".join(" " * (n - base) + t for n, t in body) + ("" if rest.strip() == "|-" else "\n")
                i = j
            elif rest.strip():
                out[key], i = scalar(rest), i + 1
            elif i + 1 < len(lines) and lines[i + 1][0] > indent:
                out[key], i = block(i + 1, lines[i + 1][0])
            elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
                out[key], i = block(i + 1, indent)
            else:
                out[key], i = None, i + 1
        return out, i

    doc, end = block(0, 0)
    if end != len(lines):
        raise ValueError(f"unparsed content at line {end}: {lines[end][1]!r}")
    return doc


def load(name):
    with open(os.path.join(BASE, name), encoding="utf-8") as handle:
        return parse(handle.read())


def content(name):
    """The manifest without comment lines (comments may name what is deliberately absent)."""
    with open(os.path.join(BASE, name), encoding="utf-8") as handle:
        return "".join(line for line in handle if not line.lstrip().startswith("#"))


def walk(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: load(name) for name in FILES}
        cls.deployment = cls.docs["deployment.yaml"]
        cls.pod = cls.deployment["spec"]["template"]["spec"]
        cls.container = cls.pod["containers"][0]

    def test_exact_resource_set(self):
        self.assertEqual(sorted(f for f in os.listdir(BASE) if f.endswith((".yaml", ".yml"))), sorted(FILES))
        kinds = {name: (doc["kind"], doc["metadata"]["name"], doc["metadata"]["namespace"])
                 for name, doc in self.docs.items()}
        self.assertEqual(kinds, {
            "serviceaccount.yaml": ("ServiceAccount", "mias-api", "mias"),
            "configmap.yaml": ("ConfigMap", "mias-api-config", "mias"),
            "pvc.yaml": ("PersistentVolumeClaim", "mias-artifacts", "mias"),
            "deployment.yaml": ("Deployment", "mias-api", "mias"),
            "service.yaml": ("Service", "mias-api", "mias"),
            "route.yaml": ("Route", "mias-api", "mias")})
        for banned in ("Secret", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding", "HorizontalPodAutoscaler",
                       "NetworkPolicy", "SecurityContextConstraints", "BuildConfig", "DeploymentConfig"):
            self.assertNotIn(banned, {doc["kind"] for doc in self.docs.values()})

    def test_deployment_shape(self):
        spec = self.deployment["spec"]
        self.assertEqual((spec["replicas"], spec["strategy"]["type"]), (1, "Recreate"))
        self.assertEqual((self.pod["serviceAccountName"], self.pod["automountServiceAccountToken"]), ("mias-api", False))
        self.assertEqual(self.pod["terminationGracePeriodSeconds"], 30)
        self.assertRegex(self.container["image"], IMAGE)                 # immutable digest, never a mutable tag
        self.assertNotIn("command", self.container)                        # the image's exec-form CMD is used
        self.assertNotIn("args", self.container)
        self.assertEqual(self.container["ports"], [{"name": "http", "containerPort": 8080, "protocol": "TCP"}])
        self.assertEqual(self.container["resources"], {"requests": {"cpu": "50m", "memory": "128Mi"},
                                                       "limits": {"cpu": "500m", "memory": "512Mi"}})

    def test_restricted_security_context(self):
        pod_sc, sc = self.pod["securityContext"], self.container["securityContext"]
        self.assertEqual(pod_sc, {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(sc, {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                              "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})
        keys = {k for doc in self.docs.values() for k, _ in walk(doc)}
        for banned in ("runAsUser", "runAsGroup", "fsGroup", "privileged", "hostNetwork", "hostPID", "hostIPC",
                       "hostPath", "add", "procMount", "seLinuxOptions"):
            self.assertNotIn(banned, keys)
        self.assertNotIn("anyuid", "".join(content(f) for f in FILES))

    def test_volumes(self):
        mounts = {m["name"]: m for m in self.container["volumeMounts"]}
        self.assertEqual(mounts["artifacts"], {"name": "artifacts", "mountPath": "/var/lib/mias/artifacts",
                                               "readOnly": True})
        self.assertEqual(mounts["tmp"], {"name": "tmp", "mountPath": "/tmp"})
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        # The claim is attached read-write (SELinux relabel and fsGroup on attach); the container mount is read-only.
        self.assertEqual(volumes["artifacts"]["persistentVolumeClaim"], {"claimName": "mias-artifacts"})
        self.assertEqual(volumes["tmp"]["emptyDir"], {"sizeLimit": "64Mi"})
        pvc = self.docs["pvc.yaml"]["spec"]
        self.assertEqual((pvc["storageClassName"], pvc["accessModes"], pvc["resources"]["requests"]["storage"]),
                         ("thin-csi", ["ReadWriteOnce"], "1Gi"))

    def test_probes(self):
        expected = {"startupProbe": ("/health/live", 2, 2, 60), "livenessProbe": ("/health/live", 20, 2, 3),
                    "readinessProbe": ("/health/ready", 10, 3, 3)}
        for probe, (path, period, timeout, failures) in expected.items():
            with self.subTest(probe=probe):
                p = self.container[probe]
                self.assertEqual(p["httpGet"], {"path": path, "port": "http"})      # no auth headers
                self.assertEqual((p["periodSeconds"], p["timeoutSeconds"], p["failureThreshold"]),
                                 (period, timeout, failures))
        self.assertEqual(self.container["readinessProbe"]["successThreshold"], 1)

    def test_configuration_and_secret_reference(self):
        config = self.docs["configmap.yaml"]["data"]
        self.assertEqual(config, {"MIAS_API_HOST": "0.0.0.0", "MIAS_API_PORT": "8080", "MIAS_API_DOCS_ENABLED": "false",
                                  "MIAS_ARTIFACT_ROOT": "/var/lib/mias/artifacts"})
        self.assertEqual(self.container["envFrom"], [{"configMapRef": {"name": "mias-api-config"}}])
        self.assertEqual(self.container["env"], [{"name": "MIAS_API_READ_TOKEN", "valueFrom": {
            "secretKeyRef": {"name": "mias-api-auth", "key": "MIAS_API_READ_TOKEN"}}}])
        text = "".join(content(f) for f in FILES)
        for banned in ("MIAS_RECEIPT_ROOT", "MIAS_API_OPERATOR_TOKEN", "MIAS_API_ACTIONS_ENABLED", "MIAS_BUILD_ID",
                       "REDIS", "DATABASE_URL", "POSTGRES", "TELEGRAM", "OPENAI", "MASSIVE", "OPTIONS_DATA",
                       "MARKET_DATA", "stringData", "kind: Secret", ":latest"):
            self.assertNotIn(banned, text)
        self.assertNotIn("value:", content("deployment.yaml"))          # no literal env values in the Deployment

    def test_service_and_route(self):
        service = self.docs["service.yaml"]["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertEqual(service["ports"], [{"name": "http", "port": 8080, "targetPort": "http", "protocol": "TCP"}])
        self.assertEqual(service["selector"], {"app.kubernetes.io/name": "mias-api"})
        route = self.docs["route.yaml"]["spec"]
        self.assertEqual(route["tls"], {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"})
        self.assertEqual((route["to"]["kind"], route["to"]["name"], route["port"]["targetPort"]),
                         ("Service", "mias-api", "http"))
        self.assertEqual(route["host"], "mias-api.apps.ngc.sirii.org")
        self.assertEqual(self.docs["serviceaccount.yaml"]["automountServiceAccountToken"], False)

    def test_labels_for_observability(self):
        labels = self.deployment["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/name"], "mias-api")
        self.assertEqual(labels["app.kubernetes.io/part-of"], "mias")
        self.assertEqual(self.deployment["spec"]["selector"]["matchLabels"], {"app.kubernetes.io/name": "mias-api"})

    def test_parser_is_strict(self):
        for bad in ("a: {b: 1}", "a:\n\tb: 1", "a: 1\na: 2", "a: [1, 2]", "a: >\n  x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse(bad)


if __name__ == "__main__":
    unittest.main()
