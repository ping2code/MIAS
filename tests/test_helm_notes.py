"""Hardening Task 3: the chart's NOTES (post-install/upgrade operator summary). Rendered offline with
`helm install --dry-run=client` (skipped if `helm` is absent); no cluster access."""
import subprocess
import unittest

from tests.test_helm_mias import CHART, HELM


def notes(*sets):
    args = [HELM, "install", "mias", CHART, "--namespace", "mias", "--dry-run=client"]
    for item in sets:
        args += ["--set", item]
    out = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout
    return out[out.index("NOTES:"):]


@unittest.skipUnless(HELM, "helm binary not available")
class HelmNotesTests(unittest.TestCase):
    def test_default_notes_point_operators_to_the_runbook_and_checks(self):
        text = notes()
        for expected in ("docs/operations-release-runbook.md", "https://mias-api.apps.ngc.sirii.org",
                         "https://mias-ui.apps.ngc.sirii.org", "/health/ready", "/healthz",
                         "oc get prometheusrule mias-alerts", "mias-publisher", "--replicas=0",
                         "Never delete the mias-artifacts PVC"):
            self.assertIn(expected, text)
        self.assertRegex(text, r"API image: \S+@sha256:[0-9a-f]{64}")
        self.assertRegex(text, r"UI image: +\S+@sha256:[0-9a-f]{64}")

    def test_notes_follow_toggles_and_hold_no_secrets(self):
        text = notes("ui.enabled=false", "monitoring.alerts.enabled=false")
        self.assertNotIn("mias-ui", text)
        self.assertNotIn("prometheusrule", text)
        for banned in ("Bearer ", "password", "MIAS_API_READ_TOKEN=", "token:"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
