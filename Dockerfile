# syntax=docker/dockerfile:1
#
# Kollektiv runtime image.
#
#   docker build -t kollektiv .
#   docker run --env-file .env -p 8000:8000 kollektiv                 # API
#   docker run --env-file .env -p 8001:8001 kollektiv \
#       python -m src.api.mcp_server --transport sse --host 0.0.0.0 # MCP
#
# The SQLite database and the agent workspace both live under /app/data, which
# docker-compose mounts as a shared volume so the API and the MCP server see the
# same projects.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WORKSPACE_DIR=/app/data/workspace \
    DATABASE_URL=sqlite:////app/data/kollektiv.db

WORKDIR /app

# setuptools resolves the packages listed in pyproject.toml, so the sources have
# to be present for the install. The pip cache mount keeps rebuilds fast.
COPY pyproject.toml README.md CHANGELOG.md ./
COPY config ./config
COPY src ./src
# The dashboard. It is also inside the wheel, but the image runs uvicorn from
# /app with the sources on disk, so `create_app` resolves the dashboard to
# /app/web — without this line the container serves an API with no /ui.
COPY web ./web

# KOLLEKTIV_EXTRAS=postgres installs the psycopg driver for a Neon/Postgres
# deployment (docker build --build-arg KOLLEKTIV_EXTRAS=postgres).
ARG KOLLEKTIV_EXTRAS=
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip \
    && python -m pip install ".${KOLLEKTIV_EXTRAS:+[$KOLLEKTIV_EXTRAS]}" \
    && rm -rf /app/*.egg-info \
    && mkdir -p /app/data/workspace \
    && useradd --create-home --uid 10001 kollektiv \
    && chown -R kollektiv:kollektiv /app

USER kollektiv

VOLUME ["/app/data"]
EXPOSE 8000 8001

# `kollektiv check` exits non-zero when something required is missing, so it is
# only used as a readiness probe for the API's HTTP surface instead.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os; \
urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"API_PORT\", \"8000\")}/health', timeout=5)"

CMD ["python", "-m", "uvicorn", "src.api.routes:app", "--host", "0.0.0.0", "--port", "8000"]
