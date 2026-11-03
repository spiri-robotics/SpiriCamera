# SpiriCamera multi-stage Docker build
#
# builder: installs runtime dependencies into /app/.venv
# dev:     adds dev dependencies and tests (compose.dev.yaml, compose.test.yaml)
# runtime: minimal image with only what's needed (the default target)

FROM python:3.14-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# Dependencies first, in their own layer, so editing src/ doesn't reinstall them.
COPY pyproject.toml uv.lock ./
# --no-default-groups: pyproject.toml names dev and docs as default groups, for
# local work; the image gets runtime dependencies only.
RUN uv sync --frozen --no-default-groups --no-install-project

# README.md is needed too: hatchling reads the readme path from pyproject.toml.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-default-groups

FROM builder AS dev

RUN uv sync --frozen --no-default-groups --group dev
COPY tests ./tests
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["python", "-m", "SpiriCamera.main"]

FROM python:3.14-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

CMD ["python", "-m", "SpiriCamera.main"]
