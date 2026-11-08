# syntax=docker/dockerfile:1.7
#
# Glow Derma backend image: one image for every Django process (api, worker, beat, migrate), selected by the
# entrypoint's first argument (entrypoint.sh). Built and pushed by .github/workflows/ci.yml as
# ghcr.io/glowdermatology/backend:backend-<sha7>; deployed by digest from the infra repository.
#
#   builder  uv installs the locked runtime dependencies (no dev group) into /app/.venv, bytecode-compiled
#   runtime  python slim, no compiler, no package manager cache, no shell tools; source owned by root and
#            read-only for the app user; static files (Django admin, Unfold, DRF) collected at build time
#
# No secret is ever baked in: collectstatic runs with config.settings.build, whose values are throwaway.

FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS builder

COPY --from=astral/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first (cached until pyproject.toml or uv.lock changes), then the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project --no-editable

# ============================================================
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS runtime

ENV DJANGO_SETTINGS_MODULE=config.settings.production \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

# Debian security updates the pinned base image does not carry yet (image-scan: Trivy and Grype fail on a fixed
# HIGH/CRITICAL), and no pip: the app runs from /app/.venv, and pip's vendored urllib3/msgpack are only scan
# findings here.
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall --yes --root-user-action=ignore pip

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . /app
RUN chmod 0755 /app/entrypoint.sh && python -m compileall -q -j 0 /app/config /app/glowderma

# Django admin, Unfold and DRF static files, served by WhiteNoise from /app/staticfiles (build-only settings).
# One line: glowderma/core/tests/test_ci_production_check.py runs this exact RUN command.
RUN DJANGO_SETTINGS_MODULE=config.settings.build python manage.py collectstatic --noinput

# Unprivileged runtime user. /app/media is the only application directory it may write (mounted from the host
# in production); /tmp is a tmpfs in production (read-only root filesystem).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
    && mkdir -p /app/media \
    && chown app:app /app/media

USER 10001:10001
EXPOSE 8000

# Liveness of gunicorn (plain health/ answers 200 while it serves). Host "localhost" is always allowed.
HEALTHCHECK --interval=15s --timeout=6s --start-period=30s --retries=6 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8000/api/v1/health/', headers={'Host': 'localhost'}), timeout=5)"]

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]
