# One image for every Beluno process; the command selects which one runs:
#   API (default)  uvicorn beluno.api.main:app
#   worker         python -m beluno.worker.main
#   scheduler      python -m beluno.scheduler.main
#   migrate        python scripts/bootstrap_database.py   (one-shot: migrations + job schema)
#
# No configuration or secret is baked in. Every BELUNO_* setting arrives through
# the environment at run time.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.11.7

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------
# Build stage: resolve the locked runtime dependencies into /app/.venv.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

# Bytecode is compiled at build time so containers start fast and can run on a
# read-only root filesystem. The interpreter comes from the base image, never a download.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# The project is installed editable (the default). beluno.db.bootstrap locates
# alembic.ini and sql/ relative to the source tree, so the runtime image keeps
# the same /app layout instead of a site-packages copy.
COPY README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY sql ./sql
COPY scripts ./scripts
RUN uv sync --locked --no-dev \
    && /app/.venv/bin/python -m compileall -q src alembic scripts

# ---------------------------------------------------------------------------
# Runtime stage: interpreter, virtualenv, and source only. No uv, no compilers.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# Fixed numeric IDs so volume ownership and orchestrator runAsUser stay predictable.
RUN groupadd --system --gid 10001 beluno \
    && useradd --system --uid 10001 --gid beluno --no-create-home --shell /usr/sbin/nologin beluno

# The application tree stays root-owned and read-only to the beluno user.
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
USER 10001:10001

EXPOSE 8000

# Liveness only: it must not depend on the database, or a database blip would
# get healthy API containers restarted. Readiness is /health/ready. Worker,
# scheduler, and migrate containers disable this check in their deployment.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

# Behind a load balancer set FORWARDED_ALLOW_IPS to the proxy addresses or CIDRs
# (uvicorn reads it when --forwarded-allow-ips is absent; the default trusts only
# loopback). Never set it to "*" on a publicly reachable port: callers could then
# spoof the client address that abuse limits key on.
CMD ["uvicorn", "beluno.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
