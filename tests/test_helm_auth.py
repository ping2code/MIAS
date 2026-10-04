"""Hardening Task 8: OpenShift OAuth for the UI through an oauth-proxy sidecar.

Stage 1 ("routeMode: path") added the proxy to both UI replicas and exposed only /oauth/* on the UI host. Stage 2
("routeMode: full", the default) points the UI Route at the proxy; the probe path, token UI and public API Route are
unchanged. "ui.oauth.enabled=false" renders chart 0.5.0 exactly. Renders with
the local `helm` binary (skipped if absent); offline.
"""
import hashlib
import json
import re
import unittest

from tests.test_helm_mias import (HELM, OAUTH_OFF, ROUTE_ANNOTATIONS, STAGE1_OAUTH_ROUTE, UI_OAUTH_OBJECTS, render,
                                  route_tls)
from tests.test_openshift_manifests import parse

HOST = "mias-ui.apps.ngc.sirii.org"
PROXY_IMAGE = ("quay.io/openshift-release-dev/ocp-v4.0-art-dev"
               "@sha256:baa393768a6fd88e5c655b1cfdafcc145d0d8465ee301e630d935604da86baa6")
SAR = {"namespace": "mias", "resource": "services", "resourceName": "mias-ui", "verb": "get"}
# Canonical hash (objects sorted, helm.sh/chart label removed) of chart 0.5.0 (main 230d717, live revision 29).
CHART_050 = "29351352ff97b4fc"
EXPECTED_ARGS = [
    "--provider=openshift", "--http-address=0.0.0.0:8081", "--https-address=", "--upstream=http://127.0.0.1:8080",
    "--openshift-service-account=mias-ui", f"--redirect-url=https://{HOST}/oauth/callback",
    "--openshift-sar=" + json.dumps(SAR, separators=(",", ":")), "--skip-auth-regex=^/healthz$",
    "--skip-provider-button=true", "--cookie-secret-file=/etc/oauth-proxy/cookie/session_secret",
    "--cookie-name=_mias_session", "--cookie-secure=true", "--cookie-httponly=true", "--cookie-samesite=lax",
    "--cookie-expire=8h", "--pass-basic-auth=false", "--pass-user-headers=false", "--pass-access-token=false",
    "--request-logging=false",
]


def canonical(objs):
    out = []
    for key in sorted(objs):
        obj = json.loads(json.dumps(objs[key]))
        obj["metadata"].get("labels", {}).pop("helm.sh/chart", None)
        out.append(obj)
    return hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()[:16]


def args_of(container):
    # The strict test parser keeps JSON escapes inside double-quoted scalars; undo them for comparison.
    return [a.replace('\\"', '"') for a in container["args"]]


@unittest.skipUnless(HELM, "helm binary not available")
class OAuthProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objs, cls.text = render()
        cls.dep = cls.objs[("Deployment", "mias-ui")]
        cls.pod = cls.dep["spec"]["template"]["spec"]
        cls.containers = {c["name"]: c for c in cls.pod["containers"]}
        cls.proxy = cls.containers["oauth-proxy"]

    # 1. the proxy renders as a sidecar of every UI replica
    def test_sidecar_in_both_replicas(self):
        self.assertEqual(list(self.containers), ["mias-ui", "oauth-proxy"])
        self.assertEqual(self.dep["spec"]["replicas"], 2)
        self.assertEqual(self.proxy["image"], PROXY_IMAGE)
        self.assertEqual(self.proxy["ports"], [{"name": "oauth", "containerPort": 8081, "protocol": "TCP"}])
        for probe in ("livenessProbe", "readinessProbe"):
            self.assertEqual(self.proxy[probe]["httpGet"], {"path": "/oauth/healthz", "port": "oauth"})
        with self.assertRaises(AssertionError):
            render("ui.oauth.image.digest=latest")

    # 2-6. one shared cookie Secret (by reference only), secure cookie, redirect URL and SA OAuth client
    def test_proxy_arguments(self):
        self.assertEqual(args_of(self.proxy), EXPECTED_ARGS)
        cookie = [v for v in self.pod["volumes"] if v["name"] == "oauth-cookie"][0]
        self.assertEqual(cookie["secret"], {"secretName": "mias-ui-oauth-cookie",
                                            "items": [{"key": "session_secret", "path": "session_secret"}]})
        self.assertNotIn(("Secret", "mias-ui-oauth-cookie"), self.objs)          # created out of band
        self.assertNotIn("kind: Secret", self.text)
        annotations = self.objs[("ServiceAccount", "mias-ui")]["metadata"]["annotations"]
        self.assertEqual(json.loads(annotations["serviceaccounts.openshift.io/oauth-redirectreference.primary"]),
                         {"kind": "OAuthRedirectReference", "apiVersion": "v1",
                          "reference": {"kind": "Route", "name": "mias-ui"}})

    def test_only_the_proxy_gets_a_service_account_token(self):
        self.assertIs(self.pod["automountServiceAccountToken"], False)
        self.assertIs(self.objs[("ServiceAccount", "mias-ui")]["automountServiceAccountToken"], False)
        sa = [v for v in self.pod["volumes"] if v["name"] == "oauth-sa"][0]["projected"]["sources"]
        self.assertEqual(sa[0], {"serviceAccountToken": {"path": "token", "expirationSeconds": 3607}})
        self.assertEqual(sa[1]["configMap"]["name"], "kube-root-ca.crt")
        self.assertEqual(sa[2]["downwardAPI"]["items"][0]["fieldRef"]["fieldPath"], "metadata.namespace")
        self.assertIn({"name": "oauth-sa", "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                       "readOnly": True}, self.proxy["volumeMounts"])
        nginx = self.containers["mias-ui"]
        self.assertEqual(nginx["volumeMounts"], [{"name": "tmp", "mountPath": "/tmp"}])   # no token, no cookie secret

    def test_proxy_security_context_and_resources(self):
        self.assertEqual(self.proxy["securityContext"], self.containers["mias-ui"]["securityContext"])
        self.assertEqual(self.proxy["securityContext"]["readOnlyRootFilesystem"], True)
        self.assertEqual(self.proxy["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertEqual(self.proxy["resources"], {"requests": {"cpu": "10m", "memory": "32Mi"},
                                                   "limits": {"cpu": "100m", "memory": "64Mi"}})
        self.assertNotIn("env", self.proxy)

    # 7. authorization: get services/mias-ui in mias, granted to the mias-viewers group only
    def test_authorization_scope(self):
        role = self.objs[("Role", "mias-ui-access")]
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["services"], "resourceNames": ["mias-ui"],
                                          "verbs": ["get"]}])
        binding = self.objs[("RoleBinding", "mias-ui-access")]
        self.assertEqual(binding["subjects"], [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group",
                                                "name": "mias-viewers"}])
        self.assertEqual(binding["roleRef"]["name"], "mias-ui-access")
        for kind in ("ClusterRole", "ClusterRoleBinding"):
            self.assertNotIn(kind, {k for k, _ in self.objs})

    # 12-13, 18. Stage 2 (default): the UI Route targets the proxy with unchanged TLS/HSTS/cookies; the public API
    # Route is untouched; the Stage 1 /oauth path Route is gone (the UI Route now carries /oauth/* too)
    def test_stage2_routes(self):
        ui = self.objs[("Route", "mias-ui")]
        self.assertEqual(ui["spec"]["port"], {"targetPort": "oauth"})
        self.assertEqual((ui["spec"]["host"], ui["spec"]["tls"]), (HOST, route_tls("mias-ui-tls")))
        self.assertNotIn("path", ui["spec"])
        self.assertEqual(ui["metadata"]["annotations"], ROUTE_ANNOTATIONS)
        self.assertNotIn(STAGE1_OAUTH_ROUTE, self.objs)
        api = self.objs[("Route", "mias-api")]
        self.assertEqual(api["spec"]["port"], {"targetPort": "http"})
        self.assertEqual(api["metadata"]["annotations"], ROUTE_ANNOTATIONS)
        off, _ = render(OAUTH_OFF)
        self.assertEqual(api, off[("Route", "mias-api")])

    # Rollback to Stage 1 (routeMode=path): UI Route back on nginx, /oauth/* only through the path Route
    def test_stage1_rollback_routes(self):
        objs, _ = render("ui.oauth.routeMode=path")
        ui = objs[("Route", "mias-ui")]
        self.assertEqual(ui["spec"]["port"], {"targetPort": "http"})
        path = objs[STAGE1_OAUTH_ROUTE]
        self.assertEqual((path["spec"]["host"], path["spec"]["path"], path["spec"]["port"]),
                         (HOST, "/oauth", {"targetPort": "oauth"}))
        for route in (ui, path):
            self.assertEqual(route["spec"]["tls"], route_tls("mias-ui-tls"))
            self.assertEqual(route["metadata"]["annotations"], ROUTE_ANNOTATIONS)
        for kind_name, obj in objs.items():                                     # nothing but the Routes differs
            if kind_name[0] != "Route":
                self.assertEqual(obj, self.objs[kind_name], kind_name)
        with self.assertRaises(AssertionError):
            render("ui.oauth.routeMode=sometimes")

    def test_router_policy_tightening_is_a_separate_switch(self):
        default = self.objs[("NetworkPolicy", "mias-ui-allow-router")]["spec"]["ingress"][0]["ports"]
        self.assertEqual(default, [{"protocol": "TCP", "port": 8080}, {"protocol": "TCP", "port": 8081}])
        objs, _ = render("ui.oauth.routerToNginx=false")
        tight = objs[("NetworkPolicy", "mias-ui-allow-router")]["spec"]["ingress"][0]["ports"]
        self.assertEqual(tight, [{"protocol": "TCP", "port": 8081}])            # nginx then unreachable directly
        objs, _ = render("ui.oauth.routeMode=path", "ui.oauth.routerToNginx=false")
        path_mode = objs[("NetworkPolicy", "mias-ui-allow-router")]["spec"]["ingress"][0]["ports"]
        self.assertEqual(path_mode, default)                                     # Stage 1 always needs 8080

    # 14-16. UI HA, API, collector and blackbox untouched
    def test_other_workloads_and_ha_unchanged(self):
        off, _ = render(OAUTH_OFF)
        for name in ("mias-api", "otel-collector", "mias-publisher", "blackbox-exporter"):
            self.assertEqual(self.objs[("Deployment", name)], off[("Deployment", name)], name)
        self.assertEqual(self.objs[("PodDisruptionBudget", "mias-ui")], off[("PodDisruptionBudget", "mias-ui")])
        self.assertEqual(self.pod["topologySpreadConstraints"],
                         off[("Deployment", "mias-ui")]["spec"]["template"]["spec"]["topologySpreadConstraints"])
        self.assertEqual(self.objs[("PrometheusRule", "mias-alerts")], off[("PrometheusRule", "mias-alerts")])
        self.assertEqual(self.objs[("Probe", "mias-ui-healthz")], off[("Probe", "mias-ui-healthz")])
        nginx_off = off[("Deployment", "mias-ui")]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(self.containers["mias-ui"], nginx_off)                # the nginx container itself unchanged

    # 17. NetworkPolicy: router may reach 8080 (Stage 1) and 8081; proxy egress only to the VIP:443 and API servers
    def test_network_policy_minimal(self):
        router = self.objs[("NetworkPolicy", "mias-ui-allow-router")]["spec"]["ingress"][0]["ports"]
        self.assertEqual(router, [{"protocol": "TCP", "port": 8080}, {"protocol": "TCP", "port": 8081}])
        egress = self.objs[("NetworkPolicy", "mias-ui-egress-oauth")]["spec"]
        self.assertEqual(egress["podSelector"], {"matchLabels": {"app.kubernetes.io/name": "mias-ui"}})
        self.assertEqual(egress["policyTypes"], ["Egress"])
        self.assertEqual(egress["egress"], [
            {"to": [{"ipBlock": {"cidr": "192.168.5.141/32"}}], "ports": [{"protocol": "TCP", "port": 443}]},
            {"to": [{"ipBlock": {"cidr": f"192.168.5.{i}/32"}} for i in (161, 162, 163)],
             "ports": [{"protocol": "TCP", "port": 6443}]}])
        self.assertNotIn("0.0.0.0/0", self.text)
        objs, _ = render("networkPolicy.enabled=false")
        self.assertNotIn(("NetworkPolicy", "mias-ui-egress-oauth"), objs)

    # 19. ui.enabled=false removes every auth resource; ui.oauth.enabled=false renders chart 0.5.0 exactly
    def test_disable_paths(self):
        objs, text = render("ui.enabled=false")
        self.assertFalse(UI_OAUTH_OBJECTS & set(objs))
        self.assertNotIn("oauth", text)
        off, _ = render(OAUTH_OFF)
        self.assertFalse(UI_OAUTH_OBJECTS & set(off))
        self.assertEqual(canonical(off), CHART_050)

    # 20. no Secret data anywhere in the render
    def test_no_secret_material(self):
        self.assertNotRegex(self.text, r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
        self.assertNotIn("session_secret:", self.text)
        self.assertNotIn("stringData", self.text)
        self.assertNotRegex(self.text, re.compile(r"cookie-secret=", re.I))      # only cookie-secret-file
        self.assertNotIn("client-secret", self.text)


if __name__ == "__main__":
    unittest.main()
