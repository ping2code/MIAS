# MIAS API container (Phase 14B). Production target: `runtime` (the default, last stage is NOT it; build with
# --target runtime). Validation target: `validation` (full requirements + tests, for regression only).
#
#   podman build --target runtime --build-arg MIAS_BUILD_ID=$(git rev-parse HEAD) \
#       -t mias-api:$(git rev-parse --short=12 HEAD) -f Containerfile .
#
# Base images are pinned by digest (Red Hat UBI 9). Python 3.14 comes from the RHEL 9 python3.14 RPM in both stages,
# so the venv built in the builder runs unchanged on the runtime interpreter. No secrets, .env or credentials are
# copied; the runtime image has no compiler, pip, tests or package-manager cache.

ARG UBI_PYTHON=registry.access.redhat.com/ubi9/python-314@sha256:31d041f22b100ededa0be994a9f89c952f21f9d969418a260916db4c1540d53d
ARG UBI_MINIMAL=registry.access.redhat.com/ubi9/ubi-minimal@sha256:eba570d04193d1523a8576b1c0ff00e681c9edb1a41d4742559b6e3ff457601e

# --- builder: pinned runtime dependencies (binary wheels only) + byte-compiled application -------------------------
FROM ${UBI_PYTHON} AS builder
USER 0
COPY requirements-api.txt /build/requirements-api.txt
RUN /usr/bin/python3.14 -m venv /opt/mias/venv \
 && /opt/mias/venv/bin/pip install --no-cache-dir --disable-pip-version-check --no-deps --only-binary=:all: \
        -r /build/requirements-api.txt \
 && /opt/mias/venv/bin/pip check \
 && /opt/mias/venv/bin/pip uninstall -y --disable-pip-version-check pip \
 && find /opt/mias/venv -name '__pycache__' -prune -exec rm -rf {} +
# Explicit allowlist: the API, the artifact store, and the pure domain packages they import.
COPY api /opt/mias/app/api
COPY artifact_store /opt/mias/app/artifact_store
COPY alert_engine /opt/mias/app/alert_engine
COPY trade_setup /opt/mias/app/trade_setup
COPY market_intelligence /opt/mias/app/market_intelligence
COPY evidence_synthesis /opt/mias/app/evidence_synthesis
COPY options_intelligence /opt/mias/app/options_intelligence
COPY market_data /opt/mias/app/market_data
# Hash-checked bytecode: valid regardless of file mtimes, so nothing is ever recompiled or written at runtime.
RUN /usr/bin/python3.14 -m compileall -q --invalidation-mode unchecked-hash /opt/mias/app /opt/mias/venv/lib

# --- runtime: production image ---------------------------------------------------------------------------------------
FROM ${UBI_MINIMAL} AS runtime
ARG MIAS_BUILD_ID=unknown
ARG MIAS_SOURCE=https://github.com/ping2code/MIAS
# python3.14: the interpreter (same RPM build as the builder). tar: required by `oc cp` for the Phase 14D publisher
# toolbox, which uses this same image.
RUN microdnf -y --nodocs --setopt=install_weak_deps=0 install python3.14 tar \
 && microdnf clean all \
 && rm -rf /var/cache/yum /var/cache/dnf /var/lib/dnf/repos /var/log/dnf* /var/log/hawkey.log
# root-owned, world-readable, not writable: works for any UID (OpenShift arbitrary UID in group 0).
COPY --from=builder --chown=0:0 /opt/mias /opt/mias
ENV PATH=/opt/mias/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONPATH=/opt/mias/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    MIAS_API_HOST=0.0.0.0 \
    MIAS_API_PORT=8080 \
    MIAS_API_DOCS_ENABLED=false \
    MIAS_BUILD_ID=${MIAS_BUILD_ID}
LABEL org.opencontainers.image.title="mias-api" \
      org.opencontainers.image.description="MIAS read API and artifact-store CLI (Phase 13 contract)" \
      org.opencontainers.image.revision="${MIAS_BUILD_ID}" \
      org.opencontainers.image.source="${MIAS_SOURCE}" \
      org.opencontainers.image.base.name="registry.access.redhat.com/ubi9/ubi-minimal"
WORKDIR /opt/mias/app
EXPOSE 8080
# Nominal non-root user; OpenShift replaces it with an arbitrary UID (group 0). Nothing depends on this value.
USER 1001
CMD ["python", "-m", "api"]

# --- validation: full requirements + tests on the runtime interpreter (never deployed) ------------------------------
FROM builder AS validation-deps
COPY requirements.txt requirements-validation.txt /build/
RUN /usr/bin/python3.14 -m venv /opt/mias/validation-venv \
 && /opt/mias/validation-venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        -r /build/requirements.txt -r /build/requirements-validation.txt

FROM runtime AS validation
USER 0
COPY --from=validation-deps --chown=0:0 /opt/mias/validation-venv /opt/mias/validation-venv
COPY --chown=0:0 . /opt/mias/src
ENV PATH=/opt/mias/validation-venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONPATH=/opt/mias/src
WORKDIR /opt/mias/src
USER 1001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
