# CER API service image.
#
# Single stage: the project has no C-extension build step of its own (its
# dependencies ship prebuilt wheels for this base image), so a multi-stage
# build would only add complexity without shrinking the image — see the
# PID's anti-bloat directive.
#
# Base is pinned by tag and expected to already be present locally on
# dell-debian (python:3.12-slim-bookworm) to avoid a slow pull. Host Python
# is 3.11.2; the application code targets >=3.11 (see pyproject.toml) and
# runs unchanged on the 3.12 image.
FROM python:3.12-slim-bookworm

ARG APP_HOME=/app
# Fixed, image-local directories that CER's runtime paths
# (CER_METADATA_DB_PATH's directory / CER_ARTIFACT_ROOT) are expected to
# resolve to once docker-compose.yml supplies those environment variables
# and mounts named volumes at these exact paths. Pre-creating and chowning
# them here is what makes a *fresh* named volume mounted here come up
# owned by the non-root runtime user (Docker seeds an empty named volume
# from the image content already present at its mount point). Nothing
# below sets these as environment defaults — the PID requires the actual
# paths to be supplied externally, never baked into the image.
ARG DATA_METADATA_DIR=/var/lib/cer/metadata
ARG DATA_ARTIFACT_DIR=/var/lib/cer/artifacts

WORKDIR ${APP_HOME}

# --- Dependency cache warm-up --------------------------------------------
# NOT the authoritative dependency list — pyproject.toml's
# [project.dependencies] is, and the real install below always resolves
# against it for real. This layer only pre-installs today's known set so
# that, in the normal case, the resolver below finds everything already
# satisfied and does no network work. It is installed before application
# source is copied so editing CER source code never invalidates this
# layer. If a dependency is later added to pyproject.toml without being
# mirrored here, the build still succeeds correctly (pip installs the
# missing package below) — the only cost is a slower rebuild until this
# list is updated to match, never a broken image.
RUN pip install --no-cache-dir \
        "pydantic>=2" \
        "fastapi>=0.110" \
        "uvicorn>=0.27" \
        "httpx>=0.27"

# --- Application source -------------------------------------------------
COPY pyproject.toml ./
COPY src ./src

# Install the project itself, resolving [project.dependencies] for real
# (no --no-deps) so pyproject.toml stays the single source of truth for
# what ends up in the image — the warm-up layer above just makes this a
# no-op resolve in the common case, not a substitute for it.
RUN pip install --no-cache-dir .

# --- Non-root runtime user ----------------------------------------------
RUN groupadd --system cer \
    && useradd --system --gid cer --home-dir "${APP_HOME}" --shell /usr/sbin/nologin cer \
    && mkdir -p "${DATA_METADATA_DIR}" "${DATA_ARTIFACT_DIR}" \
    && chown -R cer:cer "${APP_HOME}" "${DATA_METADATA_DIR}" "${DATA_ARTIFACT_DIR}"

USER cer

# Documents the default port (cer.runtime.config.DEFAULT_PORT); the actual
# bind port is controlled at runtime via CER_PORT.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('CER_PORT', '8000') + '/health', timeout=2)"]

CMD ["python", "-m", "cer.api.main"]
