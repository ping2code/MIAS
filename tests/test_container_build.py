"""Phase 14B: static guards for the container build (no Podman needed).

- Drift guard: every package pinned in requirements-api.txt that requirements.txt also pins has the identical version,
  and the API runtime set is closed (every third-party distribution the API/store import path loads is pinned).
- Containerfile invariants: digest-pinned bases, explicit package allowlist covering every first-party import of the
  runtime path, exec-form ``python -m api``, no secrets/.env copied into the runtime stage, no ``latest``.
"""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-+]+)$")


def pins(name):
    result = {}
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            match = PIN.match(line.replace("[binary]", ""))
            assert match, f"{name}: not an exact pin: {line!r}"
            result[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    return result


def containerfile():
    with open(os.path.join(ROOT, "Containerfile"), encoding="utf-8") as handle:
        return handle.read()


def runtime_stage():
    text = containerfile()
    return text.split("AS runtime", 1)[1].split("\nFROM ", 1)[0]


RUNTIME_TRACE = """
import importlib.metadata as md, json, sys
import api.app, api.__main__, artifact_store.runner, artifact_store.service, uvicorn
import uvicorn.protocols.http.h11_impl, uvicorn.lifespan.on, uvicorn.loops.asyncio
top = {n.split('.')[0] for n in sys.modules}
first = sorted(top & {d for d in __import__('os').listdir('.') if __import__('os').path.isdir(d)})
dists = set()
for name, owners in md.packages_distributions().items():
    if name in top:
        dists.update(owners)
print(json.dumps({"first_party": first, "dists": sorted(dists)}))
"""


class DriftGuardTests(unittest.TestCase):
    def test_api_pins_match_requirements(self):
        api, full = pins("requirements-api.txt"), pins("requirements.txt")
        shared = set(api) & set(full)
        self.assertTrue({"fastapi", "starlette", "pydantic", "uvicorn", "idna", "tzdata"} <= shared)
        for name in sorted(shared):
            with self.subTest(package=name):
                self.assertEqual(api[name], full[name], f"{name} drifted from requirements.txt")

    def test_api_set_is_closed_and_minimal(self):
        result = subprocess.run([sys.executable, "-c", RUNTIME_TRACE], cwd=ROOT, capture_output=True, text=True,
                                timeout=120, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        import json
        trace = json.loads(result.stdout.strip().splitlines()[-1])
        loaded = {re.sub(r"[-_.]+", "-", d).lower() for d in trace["dists"]}
        api = pins("requirements-api.txt")
        # anyio imports sniffio only if it happens to be installed (try/except); the image omits it on purpose.
        optional = {"sniffio"}
        self.assertEqual(loaded - set(api) - optional, set(),
                         "runtime imports a distribution requirements-api.txt does not pin")
        for heavy in ("pandas", "numpy", "openai", "sqlalchemy", "psycopg", "redis", "requests", "feedparser",
                      "pypdf", "exchange-calendars", "python-dotenv", "alembic", "pytest", "httpx"):
            self.assertNotIn(heavy, api)

    def test_validation_requirements_are_exact_pins(self):
        self.assertEqual(set(pins("requirements-validation.txt")),
                         {"pytest", "pluggy", "iniconfig", "packaging", "pygments"})


class ContainerfileTests(unittest.TestCase):
    def test_bases_pinned_by_digest(self):
        bases = re.findall(r"^ARG UBI_[A-Z]+=(\S+)$", containerfile(), re.M)
        self.assertEqual(len(bases), 2)
        for base in bases:
            self.assertRegex(base, r"^registry\.access\.redhat\.com/ubi9/[a-z0-9-]+@sha256:[0-9a-f]{64}$")
        self.assertNotIn(":latest", containerfile())

    def test_allowlist_covers_runtime_first_party_imports(self):
        result = subprocess.run([sys.executable, "-c", RUNTIME_TRACE], cwd=ROOT, capture_output=True, text=True,
                                timeout=120, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        import json
        first_party = set(json.loads(result.stdout.strip().splitlines()[-1])["first_party"])
        copied = set(re.findall(r"^COPY (\w+) /opt/mias/app/\1$", containerfile(), re.M))
        self.assertEqual(first_party - copied, set(), "the runtime imports a package the image does not copy")
        self.assertEqual(copied, {"api", "artifact_store", "alert_engine", "trade_setup", "market_intelligence",
                                  "evidence_synthesis", "options_intelligence", "market_data"})

    def test_runtime_stage_invariants(self):
        stage = runtime_stage()
        self.assertIn('CMD ["python", "-m", "api"]', stage)
        self.assertIn("USER 1001", stage)
        for env in ("PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "MIAS_BUILD_ID=${MIAS_BUILD_ID}"):
            self.assertIn(env, stage)
        self.assertIn('org.opencontainers.image.revision="${MIAS_BUILD_ID}"', stage)
        copies = re.findall(r"^COPY .*$", stage, re.M)
        self.assertEqual(copies, ["COPY --from=builder --chown=0:0 /opt/mias /opt/mias"])
        for banned in ("TOKEN", "SECRET", "PASSWORD", ".env", "chmod 777", "chmod -R", "pip install", "pytest"):
            self.assertNotIn(banned, stage)
        self.assertIn("microdnf clean all", stage)

    def test_dockerignore(self):
        with open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8") as handle:
            entries = {line.strip() for line in handle if line.strip() and not line.startswith("#")}
        for required in (".git", ".venv", ".env", "*.env", "runtime", "scripts", "**/__pycache__", ".pytest_cache"):
            self.assertIn(required, entries)
        for package in ("api", "artifact_store", "alert_engine", "trade_setup", "tests", "evidence"):
            self.assertNotIn(package, entries)


if __name__ == "__main__":
    unittest.main()
