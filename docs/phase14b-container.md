# Phase 14B: Container image and local validation

Phase 14B produces the `mias-api` container image, built from `Containerfile`, and validates it locally with rootless
Podman. Nothing is pushed and no cluster is touched. The Phase 13 API and artifact-store contracts are unchanged.

## Base image and Python decision

| Stage | Image (pinned by digest) | Python |
|---|---|---|
| builder | `registry.access.redhat.com/ubi9/python-314@sha256:31d041f22b100ededa0be994a9f89c952f21f9d969418a260916db4c1540d53d` (RHEL 9.8, amd64) | 3.14.7 (`python3.14-3.14.7-2.el9_8`) |
| runtime | `registry.access.redhat.com/ubi9/ubi-minimal@sha256:eba570d04193d1523a8576b1c0ff00e681c9edb1a41d4742559b6e3ff457601e` (9.8) plus the `python3.14` RPM | 3.14.7 (same RPM build) |

- **UBI ships Python 3.14:** MIAS was verified on 3.14.4, and UBI 9 provides **3.14.7**, a newer patch of the same
  minor version.
- **Gate:** I accepted it only after the validation image passed (see below).
- **Fallback:** the documented fallback, `python:3.14-slim`, was **not needed**.
- **No compatibility changes:** MIAS code was not changed for the container.

## Multi-stage design

1. **builder:** `python3.14 -m venv /opt/mias/venv`, then `pip install --no-deps --only-binary=:all:` of exactly
   `requirements-api.txt`, then `pip check`. Pip is then removed from the venv. The application is copied from an
   explicit package allowlist and byte-compiled with `--invalidation-mode unchecked-hash`, so bytecode is never
   rebuilt or written at runtime.
2. **runtime** (production, `--target runtime`):
   - **Packages:** `ubi-minimal`, plus `microdnf install python3.14 tar` with no docs and no weak dependencies, then
     `microdnf clean all` (no package cache).
   - **Contents:** `/opt/mias` copied from the builder, owned by `root:root`. Directories are 755 and files 644: world
     readable and not writable.
   - **Not included:** a compiler, pip, tests, `scripts/`, `.git` or `.env`.
3. **validation** (never deployed, `--target validation`): the runtime stage plus a separate venv holding the full
   `requirements.txt` and `requirements-validation.txt` (pytest), plus the repository and tests. It runs the
   regression on the **same interpreter** as production.

**Application allowlist:** `api`, `artifact_store`, `alert_engine`, `trade_setup`, `market_intelligence`,
`evidence_synthesis`, `options_intelligence`, `market_data`. `tests/test_container_build.py` checks that every
first-party package the runtime path imports is copied.

## Runtime requirements (`requirements-api.txt`)

Installed with `--no-deps`, so every transitive dependency is pinned explicitly:

| Kind | Packages |
|---|---|
| Direct (identical to `requirements.txt`) | fastapi 0.142.2, starlette 1.7.0, pydantic 2.13.5, uvicorn 0.54.0, idna 3.19, tzdata 2026.4 |
| Transitive | pydantic-core 2.46.5, annotated-types 0.8.0, typing-inspection 0.4.4, typing-extensions 4.16.0, annotated-doc 0.0.5, opentelemetry-api 1.45.0 (required by FastAPI; not configured), anyio 4.15.1, h11 0.16.0, click 8.5.0 |

**Excluded:** pandas, numpy, openai, SQLAlchemy, psycopg, redis, requests, feedparser, pypdf, exchange-calendars,
python-dotenv, alembic, httpx, pytest and sniffio (anyio imports sniffio only if it's installed).

The drift guard (`tests/test_container_build.py`) fails if any pin shared with `requirements.txt` differs. It also
fails if the runtime import path loads a distribution that isn't pinned.

## Arbitrary UID (OpenShift)

- **Nominal user:** `USER 1001` is only a non-root default. Nothing depends on it, and no passwd entry or file
  ownership uses it.
- **Validation UID:** the image is validated as **UID 1000770000, group 0**. Rootless Podman can only run a UID it can
  map, so the tests use:

  ```
  --uidmap 1000770000:0:1 --uidmap 0:1:1001 --gidmap 0:0:1 --gidmap 1:1:1000 --user 1000770000:0
  ```

  This keeps container root, the image-file owner, on a different id than the process, as on OpenShift.
- **Result:** `id` shows `uid=1000770000 gid=0`, umask `0022`.
- **No workarounds:** no chmod 777, no startup chown, no passwd edits, no root.

## Read-only root filesystem

- **Flags:** validated with `--read-only --read-only-tmpfs=false --tmpfs /tmp --cap-drop=ALL
  --security-opt no-new-privileges`.
- **Writable path:** the **only** one is the `/tmp` tmpfs. Every directory on the root filesystem and the artifact
  mount refuse writes (`test_03_only_tmp_is_writable`).
- **Writes:** the API writes nothing at all. No bytecode (`PYTHONDONTWRITEBYTECODE=1` plus precompiled `.pyc`), no
  logs (stdout only), no state.
- **On OpenShift:** `/tmp` should be an `emptyDir`.

## Timezone support

`ubi-minimal` ships the `tzdata` RPM with `/usr/share/zoneinfo` stripped, so `ZoneInfo("America/New_York")` **fails**
on the bare base image. The pinned `tzdata==2026.4` Python package (already in `requirements.txt`) supplies the zone
data. The tests check this inside the image.

## Why `tar` is in the image

`oc cp` needs `tar` in the target container. The Phase 14D publisher toolbox uses this same image, so `tar` is
installed (about 1 MB).

## Build

```bash
SHA=$(git rev-parse HEAD)
podman build --target runtime --build-arg MIAS_BUILD_ID=$SHA -t mias-api:${SHA:0:12} -f Containerfile .
```

```bash
podman build --target validation --build-arg MIAS_BUILD_ID=$SHA -t mias-api-validation:${SHA:0:12} -f Containerfile .
```

- **Tags:** an immutable `mias-api:<git-sha-12>`. Never `latest`.
- **Build id:** `MIAS_BUILD_ID` becomes both the image `ENV` and the label `org.opencontainers.image.revision`, and
  `/api/v1/version` reports it.
- **Other labels:** `title=mias-api`, `source=https://github.com/ping2code/MIAS`, `base.name`.
- **Image defaults:** `MIAS_API_HOST=0.0.0.0`, `MIAS_API_PORT=8080` and `MIAS_API_DOCS_ENABLED=false`. Because the
  default bind is non-loopback, **a read token is mandatory**.

## Run locally (placeholder token only)

```bash
podman run --rm -p 127.0.0.1:8080:8080 \
  --read-only --read-only-tmpfs=false --tmpfs /tmp --cap-drop=ALL --security-opt no-new-privileges \
  -v /path/to/artifacts:/var/lib/mias/artifacts:ro \
  -e MIAS_ARTIFACT_ROOT=/var/lib/mias/artifacts \
  -e MIAS_API_READ_TOKEN='<placeholder-read-token-32+-chars>' \
  mias-api:<sha12>
```

- **Health:** `curl :8080/health/live`, then `curl :8080/health/ready`. An empty valid store is Ready.
- **Reads:** `curl -H "Authorization: Bearer <token>" :8080/api/v1/version`.
- **Receipts:** `MIAS_RECEIPT_ROOT` is not set (Phase 14 v1), so `/alerts/{id}/deliveries` returns
  `503 dependency_unavailable`, by contract.
- **Without a token:** the container exits 2 with
  `MIAS_API_READ_TOKEN is required when MIAS_API_HOST is not a loopback address`.

**Publisher toolbox** (the same image, the store mounted read-write):

```bash
podman run --rm -v /path/to/artifacts:/var/lib/mias/artifacts:rw -v /path/to/MI.json:/src/MI.json:ro \
  -e MIAS_ARTIFACT_ROOT=/var/lib/mias/artifacts mias-api:<sha12> \
  python -m artifact_store.runner publish --kind market-intelligence --file /src/MI.json
```

## Shutdown

`CMD ["python", "-m", "api"]` is exec form, so Python is PID 1 with no shell. `podman stop` (SIGTERM) leads to a
graceful uvicorn shutdown and exit code 0 in about a second. The background refresher stops too.

## Validation performed

| Check | Result |
|---|---|
| `tests/test_container_build.py` (drift guard and Containerfile invariants) | Host: pass |
| `tests/test_container_runtime.py` (Podman matrix, opt-in through `MIAS_CONTAINER_IMAGE`): arbitrary UID, read-only root, read-only store, `/tmp` the only writable path, tz, tar, no pip/gcc/pytest/caches, token required for a non-loopback bind, live/ready on an empty store, 401/200 bearer, docs 404, no `Server` header, token not in logs, SIGTERM exit 0, publish through the toolbox then read and get canonical bytes identical, restart on the same volume, missing store stays alive and not ready, metadata and history free of secrets | 7/7 pass |
| Phase 13 focused tests plus container build tests inside the validation image (UID 1000770000, read-only, no network) | 96 passed |
| Full regression inside the validation image (Python 3.14.7, UID 1000770000, read-only root, disposable PostgreSQL/Redis) | Only environment-caused differences; nothing attributable to Python 3.14.7 (below) |

### In-image regression findings

The first in-image run reported 310 failures. Every one was traced to the validation environment or a system library,
not to Python 3.14.7. I isolated each cause in turn:

1. **`/dev/shm` read-only** (`--read-only-tmpfs=false`). Multiprocessing primitives in the persistence tests need
   it. A `--tmpfs /dev/shm` cleared every `*_postgres` failure.
   - **API impact:** none. The API uses no multiprocessing, the hardened Podman matrix passes with `/dev/shm` read-only,
     and OpenShift always provides `/dev/shm`.
2. **SQLite 3.34.1** (the RHEL 9 system library) lacks `INSERT … RETURNING`, which needs 3.35 or later. The host has
   3.46.1. MIAS persistence uses `RETURNING`; production uses PostgreSQL, and SQLite is a dev/test opt-in only.
   Preloading a throwaway SQLite 3.46.1 build into the validation container (`LD_LIBRARY_PATH`, validation only)
   made **all** the persistence tests pass on Python 3.14.7.
   - **API impact:** none. The runtime image contains no persistence code.
   - **Future rule:** containerizing persistence with SQLite needs SQLite 3.35 or later.
3. **No `git` or `.git`** in the validation image (excluded from the build context by design). Two tests read
   commit metadata: `test_only_the_pinned_production_protocol` and `test_plan_without_network`. With `git-core` and
   the repository metadata mounted, both pass on 3.14.7.

After those three, the only failures are the known host baseline: Fed runpy and SEC argv. The geopolitical
`DurableIdentityLookup` test, which also fails intermittently on main, failed in the first run and passed in the
re-run.

**Image sizes:**

| Image | Size |
|---|---|
| runtime | about 215 MB |
| validation (not deployed) | about 479 MB |
| `ubi9/python-314` builder base | 1.08 GB; never shipped |

## Limits and handoff to 14C

- **Registry:** none is chosen yet, and nothing is pushed. Options for 14C:
  - **OpenShift internal registry:** Managed, PVC-backed, but no default route, so it needs an approved route-enable
    change or an in-cluster push path.
  - **Nexus:** check that it's reachable from the cluster nodes, then use an `imagePullSecret`.
- **Architecture:** the image is `amd64` only, which matches the cluster's RHCOS x86_64 nodes.
- **Base image updates:** the base images are digest-pinned. Refreshing them is a deliberate rebuild and regression,
  not automatic.
- **Python patch:** 3.14.7 in the image versus 3.14.4 on the developer host. Keep the full regression inside the image
  as the gate whenever the base image changes.
