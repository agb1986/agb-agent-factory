# The factory image: `docker build -t agb-agent-factory .`, then `docker run --rm agb-agent-factory --version`.
#
# Stage one installs the locked dependencies and the project into a virtualenv with uv. Stage two
# copies only that virtualenv and the data the commands read from the working directory. No key is
# built in: ANTHROPIC_API_KEY and TYPESAFE_API_KEY arrive at run time through an env file, and
# scripts/scan_image.py fails CI if a key-like string ends up in a layer.

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project --extra jev --extra api

# The project itself, installed as a regular package rather than editable.
COPY api ./api
COPY evals ./evals
COPY factory ./factory
COPY pipelines ./pipelines
COPY registry ./registry
COPY runtime ./runtime
COPY tools ./tools
RUN uv sync --locked --no-dev --no-editable --extra jev --extra api


FROM python:3.14-slim

# git: every review reads the change with it. ripgrep: the reviewers' `rg.search` tool.
# docker-cli: only for the fix loop, which starts sibling sandbox containers; off by default,
# see docs/deploy.md before turning it on.
ARG WITH_DOCKER_CLI=0
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && if [ "$WITH_DOCKER_CLI" = "1" ]; then apt-get install -y --no-install-recommends docker-cli; fi \
    && rm -rf /var/lib/apt/lists/* \
    # A repository mounted from the host belongs to another uid, which git refuses to read unless
    # told otherwise. Reviews only read, and the container is the trust boundary.
    && git config --system --add safe.directory '*'

# A fixed uid, so the owner of a bind-mounted data directory can be set to match (docs/deploy.md).
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin factory
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FACTORY_TRACE_DB=/app/data/traces.duckdb \
    # typesafe_sdk logs each request id at INFO. Keep it at WARNING so logs and traces stay free
    # of them; never DEBUG, which logs request bodies (docs/CONVENTIONS.md, Deployment).
    TYPESAFE_LOG_LEVEL=warning

# Read from the working directory at run time: the agent manifests and the fixture sources.
COPY agents ./agents
COPY fixtures ./fixtures

# The fixtures are built here, as the user that runs the factory, because git refuses a repository
# owned by someone else. `data/` is the volume: traces and anything else the factory keeps.
RUN mkdir -p data fixtures/repos \
    && chown -R factory:factory data fixtures/repos
USER factory
RUN python -m fixtures.build

VOLUME /app/data

ENTRYPOINT ["factory"]
CMD ["--help"]
