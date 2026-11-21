FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

FROM docker:28-cli AS docker-cli

FROM python:3.12-slim-bookworm AS python-runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && git config --system --add safe.directory /workspaces/project \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv==0.9.7 \
    && uv sync --locked --extra web --no-dev --no-install-project

FROM python-runtime AS workbench
COPY src ./src
RUN uv sync --locked --extra web --no-dev --no-editable
COPY --from=frontend /build/dist /app/static
ENTRYPOINT ["python", "-m", "stellarweaver.web"]

FROM python-runtime AS verification
RUN uv sync --locked --extra web --no-dev --group verification --no-install-project

FROM workbench AS isolated-worker
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
