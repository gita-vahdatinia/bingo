# Single-stage; the app is small and the dependency layer caches well.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app

# Dependencies before source, so editing a template doesn't reinstall FastAPI.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Snapshot lives on a mounted volume when the host provides one; without a mount
# this is still a valid path, it just doesn't survive a restart.
ENV LINEUP_DATA=/data/events.json
RUN mkdir -p /data && useradd --create-home --uid 10001 app && chown -R app:app /data
USER app

EXPOSE 8000

# One worker, always. The store is in-process with a thread lock, so a second
# worker would serve a second, divergent copy of every game.
CMD ["sh", "-c", "exec uvicorn lineup.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
