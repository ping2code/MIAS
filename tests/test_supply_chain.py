"""Hardening Task 6: the image supply-chain evidence (docs/release-evidence) is complete, consistent with the deployed
Helm pins, and free of secret material. Offline: no registry or cluster access. SBOM files are checked only if the
operator archive (outside git) is present on this machine."""
import base64
import hashlib
import os
import re
import subprocess
import unittest

from tests.test_openshift_manifests import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE = os.path.join(ROOT, "docs", "release-evidence")
MANIFEST = os.path.join(EVIDENCE, "image-provenance.yaml")
PUBKEY = os.path.join(EVIDENCE, "mias-release.pub")
VALUES = os.path.join(ROOT, "deploy", "helm", "mias", "values.yaml")
SBOM_ARCHIVE = os.path.expanduser("~/.mias-release-evidence/sbom")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
IN_CLUSTER = "image-registry.openshift-image-registry.svc:5000/"


def load():
    with open(MANIFEST, encoding="utf-8") as handle:
        return parse(handle.read())


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)


class ProvenanceManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load()
        cls.images = {i["component"]: i for i in cls.doc["images"]}

    def test_schema_and_both_mias_images(self):
        self.assertEqual(self.doc["schemaVersion"], 1)
        self.assertEqual(set(self.images), {"mias-api", "mias-ui"})
        for name, image in self.images.items():
            self.assertEqual(image["builtBy"], "mias", name)
            for key in ("repository", "tag", "digest", "sourceCommit", "ociRevision", "ociSource", "configId",
                        "baseImage", "signature", "sbom"):
                self.assertIn(key, image, name)
            self.assertEqual(image["repository"], IN_CLUSTER + "mias/" + name)
        self.assertEqual(self.doc["signing"]["method"], "cosign-key")
        self.assertEqual(self.doc["signing"]["transparencyLog"], "none")
        self.assertEqual((self.doc["sbom"]["format"], self.doc["sbom"]["specVersion"]), ("CycloneDX", "1.7"))

    def test_digests_and_commits_are_immutable_references(self):
        for name, image in self.images.items():
            for key in ("digest", "configId"):
                self.assertRegex(image[key], DIGEST, name)
            self.assertRegex(image["signature"]["attachmentDigest"], DIGEST, name)
            self.assertRegex(image["sbom"]["attestationDigest"], DIGEST, name)
            self.assertRegex(image["sourceCommit"], SHA1, name)
            self.assertEqual(image["ociRevision"], image["sourceCommit"], name)
            self.assertTrue(image["sourceCommit"].startswith(image["tag"]), name)   # tags are the commit's sha12
        for item in self.doc["unsignedRetained"] + self.doc["thirdParty"]:
            for key in ("digest", "mirroredDigest", "configId"):
                if key in item:
                    self.assertRegex(item[key], DIGEST, item["component"])
        tool_images = (self.doc["signing"]["toolImage"], self.doc["sbom"]["toolImage"])
        for ref in tool_images:
            self.assertRegex(ref.split("@", 1)[1], DIGEST)                  # tools pinned by digest, too

    def test_source_commits_are_on_main_history(self):
        if git("rev-parse", "--git-dir").returncode != 0:
            self.skipTest("not a git checkout")
        for name, image in self.images.items():
            result = git("cat-file", "-t", image["sourceCommit"])
            if result.returncode != 0:
                self.skipTest("shallow clone without the source commit")
            self.assertEqual(result.stdout.strip(), "commit", name)

    def test_manifest_matches_the_helm_pins(self):
        with open(VALUES, encoding="utf-8") as handle:
            values = parse(handle.read())
        self.assertEqual(values["image"]["digest"], self.images["mias-api"]["digest"])
        self.assertEqual(values["ui"]["image"]["digest"], self.images["mias-ui"]["digest"])
        self.assertEqual(values["ui"]["image"]["versionLabel"], self.images["mias-ui"]["tag"])
        self.assertTrue(self.images["mias-api"]["sourceCommit"].startswith(values["image"]["versionLabel"]))
        third = {t["component"]: t for t in self.doc["thirdParty"]}
        self.assertEqual(values["observability"]["collector"]["image"]["digest"],
                         third["otel-collector-contrib"]["mirroredDigest"])
        self.assertEqual(values["syntheticMonitoring"]["image"]["digest"], third["blackbox-exporter"]["mirroredDigest"])

    def test_signatures_bind_the_digest_and_in_cluster_identity(self):
        for name, image in self.images.items():
            sig = image["signature"]
            self.assertEqual(sig["identity"], image["repository"], name)      # what CRI-O sees in the cluster
            self.assertEqual(sig["attachmentTag"], image["digest"].replace(":", "-") + ".sig", name)
            self.assertEqual(image["sbom"]["attestationTag"], image["digest"].replace(":", "-") + ".att", name)
            self.assertIs(sig["cosignVerified"], True, name)
            self.assertIs(sig["containersPolicyVerified"], True, name)
            self.assertIs(image["sbom"]["attestationVerified"], True, name)

    def test_sbom_records(self):
        for name, image in self.images.items():
            sbom = image["sbom"]
            self.assertRegex(sbom["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertEqual(sbom["file"], f"{name}-{image['digest'].replace(':', '-')}.cdx.json")
            self.assertEqual(sbom["packageCount"], sbom["rpmPackages"] + sbom["pypiPackages"], name)
            self.assertGreater(sbom["packageCount"], 0, name)

    @unittest.skipUnless(os.path.isdir(SBOM_ARCHIVE), "operator SBOM archive not present on this machine")
    def test_archived_sbom_checksums(self):
        import json
        for name, image in self.images.items():
            path = os.path.join(SBOM_ARCHIVE, image["sbom"]["file"])
            with open(path, "rb") as handle:
                data = handle.read()
            self.assertEqual(hashlib.sha256(data).hexdigest(), image["sbom"]["sha256"], name)
            self.assertEqual(len(data), image["sbom"]["sizeBytes"], name)
            bom = json.loads(data)
            self.assertEqual((bom["bomFormat"], bom["specVersion"]), ("CycloneDX", "1.7"))
            self.assertEqual(bom["metadata"]["component"]["version"], image["digest"], name)
            libraries = [c for c in bom["components"] if c.get("type") == "library"]
            self.assertEqual(len(libraries), image["sbom"]["packageCount"], name)

    def test_third_party_images_are_marked_mirrored(self):
        third = {t["component"]: t for t in self.doc["thirdParty"]}
        self.assertEqual(set(third), {"otel-collector-contrib", "blackbox-exporter"})
        for name, item in third.items():
            self.assertEqual((item["builtBy"], item["mirrored"], item["signedByMias"]), ("upstream", True, False), name)
            self.assertIs(item["upstreamConfigMatch"], True, name)
            self.assertNotIn(":latest", item["upstreamImage"], name)
        self.assertFalse(set(third) & set(self.images))

    def test_unsigned_rollback_digests_are_listed(self):
        for item in self.doc["unsignedRetained"]:
            self.assertIn(item["component"], self.images)
            self.assertNotEqual(item["digest"], self.images[item["component"]]["digest"])


class PublicKeyAndSecretHygieneTests(unittest.TestCase):
    def test_public_key_matches_the_recorded_fingerprint(self):
        with open(PUBKEY, encoding="ascii") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("-----BEGIN PUBLIC KEY-----\n"))
        self.assertNotIn("PRIVATE", text)
        body = "".join(line for line in text.splitlines() if not line.startswith("-----"))
        der = base64.b64decode(body)
        self.assertEqual(hashlib.sha256(der).hexdigest(), load()["signing"]["publicKeyFingerprintSha256"])
        self.assertEqual(len(der), 91)                                      # an EC P-256 SubjectPublicKeyInfo

    def test_no_private_key_material_is_tracked(self):
        result = git("ls-files", "-z")
        if result.returncode != 0:
            self.skipTest("not a git checkout")
        markers = re.compile(rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----|ENCRYPTED (?:SIGSTORE|COSIGN) PRIVATE KEY")
        offenders = []
        for path in filter(None, result.stdout.split("\0")):
            full = os.path.join(ROOT, path)
            if path.endswith((".key", ".pem", "auth.json", ".passphrase")) and "release-evidence" in path:
                offenders.append(path)
            if not os.path.isfile(full) or os.path.getsize(full) > 5_000_000:
                continue
            with open(full, "rb") as handle:
                if markers.search(handle.read()):
                    offenders.append(path)
        offenders = [p for p in offenders if p != "tests/test_supply_chain.py"]   # this file holds the patterns
        self.assertEqual(offenders, [])
        self.assertEqual(sorted(os.listdir(EVIDENCE)), ["image-provenance.yaml", "mias-release.pub"])

    def test_no_latest_image_references_in_the_chart(self):
        for directory, _, files in os.walk(os.path.join(ROOT, "deploy", "helm", "mias")):
            for name in files:
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    self.assertNotRegex(handle.read(), r"image:.*:latest\b", name)

    def test_docs_reference_the_signing_workflow(self):
        with open(os.path.join(ROOT, "docs", "hardening-image-signing-sbom.md"), encoding="utf-8") as handle:
            doc = handle.read()
        with open(os.path.join(ROOT, "docs", "operations-release-runbook.md"), encoding="utf-8") as handle:
            runbook = handle.read()
        for text in (doc, runbook):
            for needle in ("cosign", "image-provenance.yaml", "--tlog-upload=false", "verify"):
                self.assertIn(needle, text)
        fingerprint = load()["signing"]["publicKeyFingerprintSha256"]
        self.assertIn(fingerprint, doc)
        self.assertIn("MatchRepository", doc)


if __name__ == "__main__":
    unittest.main()
