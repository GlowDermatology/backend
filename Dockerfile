# syntax=docker/dockerfile:1
FROM python:3.14-slim-trixie AS builder

COPY --from=docker.io/astral/uv:0.13.5 \
    /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_CACHE_DIR=/root/.cache/uv

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-editable --no-install-project

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


# ============================================================
# Stage 2: Production Runtime
# ============================================================
FROM python:3.14-slim-trixie

# Install interactive shell utilities
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bash-completion \
        less \
    && rm -rf /var/lib/apt/lists/*

ENV DJANGO_SETTINGS_MODULE=config.settings.production \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    HISTFILE=/home/app/.bash_history \
    HISTSIZE=10000 \
    HISTFILESIZE=20000

WORKDIR /app

# Copy Python virtual environment
COPY --from=builder /app/.venv /app/.venv

# Copy application source
COPY . /app

# Copy entrypoint and normalize line endings
COPY --chmod=0755 docker/entrypoint.sh \
    /app/docker/entrypoint.sh

RUN sed -i 's/\r$//' /app/docker/entrypoint.sh

# ============================================================
# Django Static Files
# ============================================================
# Build-only placeholders, NOT production credentials
# ============================================================
# Django Static Files
# ============================================================
# One line: glowderma/core/tests/test_ci_production_check.py runs this exact RUN command.
RUN DJANGO_SETTINGS_MODULE=config.settings.build python manage.py collectstatic --noinput

# ============================================================
# Non-root Application User
# ============================================================
RUN useradd \
        --uid 10001 \
        --create-home \
        --shell /bin/bash \
        app \
    && mkdir -p /app/media \
    && chown -R app:app /app/media

# ============================================================
# Bash Configuration
# ============================================================
COPY --chown=app:app docker/bashrc /home/app/.bashrc

RUN touch /home/app/.bash_history \
    && chown app:app /home/app/.bash_history \
    && chmod 600 /home/app/.bash_history

# ============================================================
# Runtime
# ============================================================
USER app

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["api"]
