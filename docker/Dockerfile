# SpiriCamera multi-stage Docker build
#
# Build stage: install dependencies
# Runtime stage: minimal image with only what's needed

FROM python:3.14-slim AS builder

WORKDIR /app

# Install uv
RUN pip install uv==0.8.0

# Copy project config and install dependencies
# --extra ui pulls in nicegui, needed by the camera-debug-ui app
# (python -m SpiriCamera.ui); the ingress app doesn't use it but the
# two share this one image.
# README.md and src are needed here too: hatchling's build backend reads
# the readme path from pyproject.toml and needs the package importable
# to resolve the editable install.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra ui --link-mode=copy

# Runtime stage
FROM python:3.14-slim

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source code
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "SpiriCamera.main"]
