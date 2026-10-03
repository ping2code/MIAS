"""Phase 14B: the local Podman validation matrix for the production image (opt-in).

Runs only when ``MIAS_CONTAINER_IMAGE`` names a locally built runtime image (and ``MIAS_CONTAINER_BUILD_ID`` its
expected build id). Every container runs as an arbitrary OpenShift-style UID (1000770000, group 0) with a read-only
root filesystem, a tmpfs ``/tmp`` only, no capabilities, and the artifact store mounted read-only. The token is a
clearly fake placeholder. Nothing is pushed and no cluster is touched.

    MIAS_CONTAINER_IMAGE=localhost/mias-api:<sha12> MIAS_CONTAINER_BUILD_ID=<sha> \\
        python -m pytest -q tests/test_container_runtime.py
"""
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

IMAGE = os.environ.get("MIAS_CONTAINER_IMAGE")
BUILD_ID = os.environ.get("MIAS_CONTAINER_BUILD_ID", "")
TOKEN = "fake-placeholder-read-token-0123456789-not-a-secret"
UID = 1000770000
# Rootless Podman can only run a UID it can map: map container UID 1000770000 to the invoking user and keep container
# root (the image's file owner) on a different subordinate id, so the process is NOT the owner of the image files.
USER = ["--uidmap", f"{UID}:0:1", "--uidmap", "0:1:1001", "--gidmap", "0:0:1", "--gidmap", "1:1:1000",
        "--user", f"{UID}:0"]
HARDENED = ["--read-only", "--read-only-tmpfs=false", "--tmpfs", "/tmp:rw,size=16m,mode=1777", "--cap-drop=ALL",
            "--security-opt", "no-new-privileges"]
STORE = "/var/lib/mias/artifacts"


def podman(*args, check=True, timeout=120):
    result = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"podman {' '.join(args[:3])} failed: {result.stderr[-500:]}")
    return result


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, path, token=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


@unittest.skipUnless(IMAGE, "set MIAS_CONTAINER_IMAGE to a locally built mias-api runtime image")
class ContainerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = os.path.join(cls.tmp.name, "artifacts")
        cls.sources = os.path.join(cls.tmp.name, "sources")
        os.mkdir(cls.store)
        os.mkdir(cls.sources)
        cls.containers = []

    @classmethod
    def tearDownClass(cls):
        for name in cls.containers:
            podman("rm", "-f", name, check=False)
        cls.tmp.cleanup()

    def start(self, *env_overrides, token=TOKEN, wait=True):
        name, port = f"mias-api-test-{uuid.uuid4().hex[:10]}", free_port()
        env = ["-e", "MIAS_API_HOST=0.0.0.0", "-e", "MIAS_API_PORT=8080", "-e", "MIAS_API_DOCS_ENABLED=false",
               "-e", f"MIAS_ARTIFACT_ROOT={STORE}"] + (["-e", f"MIAS_API_READ_TOKEN={token}"] if token else [])
        for item in env_overrides:
            env += ["-e", item]
        podman("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080", *USER, *HARDENED,
               "-v", f"{self.store}:{STORE}:ro", *env, IMAGE)
        self.containers.append(name)
        if wait:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if http(port, "/health/live")[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            else:
                raise AssertionError(podman("logs", name, check=False).stderr[-1000:])
        return name, port

    def stop(self, name):
        started = time.monotonic()
        podman("stop", "-t", "20", name)
        elapsed = time.monotonic() - started
        state = json.loads(podman("inspect", name).stdout)[0]["State"]
        logs = podman("logs", name).stderr + podman("logs", name).stdout
        return state["ExitCode"], elapsed, logs

    def exec_sh(self, script):
        return podman("run", "--rm", *USER, *HARDENED, "-v", f"{self.store}:{STORE}:ro", "--entrypoint", "sh",
                      IMAGE, "-c", script)

    def publish(self, kind, obj):
        path = os.path.join(self.sources, f"{uuid.uuid4().hex}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, indent=2)
        # The publisher toolbox: the same image, the store mounted read-write, the source mounted read-only.
        result = podman("run", "--rm", *USER, *HARDENED, "-v", f"{self.store}:{STORE}:rw",
                        "-v", f"{path}:/src/source.json:ro", "-e", f"MIAS_ARTIFACT_ROOT={STORE}", IMAGE,
                        "python", "-m", "artifact_store.runner", "publish", "--kind", kind, "--file",
                        "/src/source.json")
        return json.loads(result.stdout)

    def test_01_image_metadata_and_contents(self):
        info = json.loads(podman("image", "inspect", IMAGE).stdout)[0]
        config, labels = info["Config"], info["Config"]["Labels"]
        self.assertEqual(config["User"], "1001")
        self.assertEqual(config["Cmd"], ["python", "-m", "api"])
        self.assertIn("8080/tcp", config.get("ExposedPorts", {}))
        env = dict(item.split("=", 1) for item in config["Env"])
        self.assertEqual((env["PYTHONDONTWRITEBYTECODE"], env["PYTHONUNBUFFERED"], env["MIAS_API_DOCS_ENABLED"]),
                         ("1", "1", "false"))
        self.assertNotIn("MIAS_API_READ_TOKEN", env)
        if BUILD_ID:
            self.assertEqual((env["MIAS_BUILD_ID"], labels["org.opencontainers.image.revision"]), (BUILD_ID, BUILD_ID))
        self.assertEqual(labels["org.opencontainers.image.title"], "mias-api")
        history = podman("history", "--no-trunc", "--format", "{{.CreatedBy}}", IMAGE).stdout
        inspect_text = json.dumps(info)
        for secret in ("TOKEN=", "PASSWORD", "SECRET=", "API_KEY", ".env", "BEGIN PRIVATE KEY", "ghp_", "Authorization"):
            self.assertNotIn(secret, history + inspect_text)
        listing = self.exec_sh("ls -a /opt/mias/app; ls /opt/mias; command -v pip pip3 gcc cc pytest || true; "
                               "ls /var/cache/yum /var/cache/dnf 2>/dev/null | wc -l").stdout.split()
        self.assertEqual(sorted(x for x in listing[:12] if x not in (".", "..")),
                         ["alert_engine", "api", "artifact_store", "evidence_synthesis", "market_data",
                          "market_intelligence", "options_intelligence", "trade_setup"])
        for absent in ("tests", "scripts", ".env", ".git", "pip", "pip3", "gcc", "pytest", "evidence"):
            self.assertNotIn(absent, listing[:-1])

    def test_02_runtime_imports_tz_tar_identity(self):
        out = self.exec_sh(
            "id -u; id -g; umask; command -v tar; "
            "python -c 'import sys, zoneinfo, api.app, artifact_store.runner, uvicorn; "
            "print(sys.version.split()[0]); print(zoneinfo.ZoneInfo(\"America/New_York\"))'; "
            "python -m artifact_store.runner --help >/dev/null && echo runner-ok").stdout.split()
        self.assertEqual(out[:4], [str(UID), "0", "0022", "/usr/bin/tar"])
        self.assertTrue(out[4].startswith("3.14."))
        self.assertEqual(out[5:], ["America/New_York", "runner-ok"])

    def test_03_only_tmp_is_writable(self):
        script = ("for d in / /opt /opt/mias /opt/mias/app /opt/mias/app/api /opt/mias/venv /etc /usr /var /var/tmp "
                  f"/run {STORE} /tmp; do if touch $d/.w 2>/dev/null; then echo W:$d; rm -f $d/.w; fi; done; "
                  "find / -xdev -writable -type d 2>/dev/null | grep -v '^/proc' | sort")
        out = self.exec_sh(script).stdout.split()
        self.assertEqual([x for x in out if x.startswith("W:")], ["W:/tmp"])
        self.assertEqual([x for x in out if not x.startswith("W:")], [])   # nothing writable on the root filesystem

    def test_04_non_loopback_without_token_refuses_to_start(self):
        result = podman("run", "--rm", *USER, *HARDENED, "-e", "MIAS_API_HOST=0.0.0.0", IMAGE, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("MIAS_API_READ_TOKEN is required", result.stderr)

    def test_05_empty_store_health_auth_and_shutdown(self):
        name, port = self.start()
        status, headers, body = http(port, "/health/live")
        self.assertEqual((status, json.loads(body)), (200, {"status": "live"}))
        self.assertNotIn("server", {k.lower() for k in headers})
        status, _, body = http(port, "/health/ready")
        self.assertEqual(status, 200, body)
        self.assertEqual({c["name"] for c in json.loads(body)["checks"]}, {"settings", "artifact_root",
                                                                           "artifact_index"})   # no receipt root
        self.assertEqual(http(port, "/api/v1/version")[0], 401)
        status, _, body = http(port, "/api/v1/version", TOKEN)
        self.assertEqual(status, 200)
        if BUILD_ID:
            self.assertEqual(json.loads(body)["build"], BUILD_ID)
        self.assertEqual(http(port, "/docs")[0], 404)
        self.assertEqual(http(port, "/openapi.json")[0], 404)
        self.assertEqual(http(port, "/api/v1/alerts", TOKEN)[0], 200)
        code, elapsed, logs = self.stop(name)
        self.assertEqual(code, 0, logs[-800:])                       # SIGTERM -> uvicorn graceful shutdown
        self.assertLess(elapsed, 15)
        self.assertIn("Finished server process", logs)
        self.assertNotIn(TOKEN, logs)

    def test_06_publish_read_canonical_and_restart(self):
        from tests.test_artifact_store import samples
        objects = samples()
        alert, mi = objects["alert"][0], objects["market-intelligence"][2]
        self.assertEqual(self.publish("alert", alert)["result"], "PUBLISHED")
        self.assertEqual(self.publish("market-intelligence", mi)["result"], "PUBLISHED")
        self.assertEqual(self.publish("alert", alert)["result"], "ALREADY_PRESENT")
        stored = os.path.join(self.store, "alert", alert["alert_id"][7:] + ".json")
        with open(stored, "rb") as handle:
            stored_bytes = handle.read()
        answers = []
        for _ in range(2):                                            # start, then restart on the same volume
            name, port = self.start()
            self.assertEqual(http(port, "/health/ready")[0], 200)
            status, headers, body = http(port, f"/api/v1/alerts/{alert['alert_id']}/canonical", TOKEN)
            self.assertEqual((status, body), (200, stored_bytes))
            self.assertEqual(headers.get("etag") or headers.get("ETag"), f'"{alert["alert_id"]}"')
            latest = json.loads(http(port, "/api/v1/market-intelligence/latest?symbol=META", TOKEN)[2])
            answers.append(latest["data"]["intelligence_id"])
            status, _, body = http(port, f"/api/v1/alerts/{alert['alert_id']}/deliveries", TOKEN)
            self.assertEqual((status, json.loads(body)["error"]["code"]), (503, "dependency_unavailable"))
            self.assertEqual(self.stop(name)[0], 0)
        self.assertEqual(answers, [mi["intelligence_id"]] * 2)
        self.assertEqual(os.stat(stored).st_mode & 0o777, 0o444)

    def test_07_missing_store_stays_alive_not_ready(self):
        name, port = self.start(f"MIAS_ARTIFACT_ROOT={STORE}/does-not-exist")
        self.assertEqual(http(port, "/health/live")[0], 200)
        status, _, body = http(port, "/health/ready")
        self.assertEqual(status, 503)
        self.assertNotIn(STORE, body.decode())
        state = json.loads(podman("inspect", name).stdout)[0]["State"]
        self.assertTrue(state["Running"])
        self.assertEqual(self.stop(name)[0], 0)


if __name__ == "__main__":
    unittest.main()
