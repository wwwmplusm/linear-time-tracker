FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM python:3.11-slim

WORKDIR /app
COPY --from=uv /uv /bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project && \
    mkdir -p /var/lib/tracker && chown 10001:10001 /var/lib/tracker
COPY tracker ./tracker

USER 10001:10001
CMD ["/app/.venv/bin/uvicorn", "tracker.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
