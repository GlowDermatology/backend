# syntax=docker/dockerfile:1
FROM python:3.14-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_CACHE_DIR=/root/.cache/uv
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-editable --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie
ENV DJANGO_SETTINGS_MODULE=config.settings.production PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY . /app
COPY --chmod=0755 docker/entrypoint.sh /app/docker/entrypoint.sh
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh
# collectstatic imports the production settings: build-only values, with the keys production refuses to
# see committed generated per build (they never reach the runtime image environment).
RUN export PUBLIC_MEDIA_BASE_URL=https://media.invalid/media/ SMS_CODE_HMAC_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" LOG_PII_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" PAYMENT_REF_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" QR_HMAC_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" BTS_WEBHOOK_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')" CLICK_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(16))')" PAYME_KEY="$(python -c 'import secrets; print(secrets.token_hex(16))')" SECRETBOX_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" && SECRET_KEY=build-only-build-only-build-only-0000 JWT_SIGNING_KEYS='{"build":"YnVpbGQtb25seS1rZXktbm90LXVzZWQtYXQtcnVudGltZSEh"}' JWT_ACTIVE_KID=build INTERNAL_API_SECRET=build-only-build-only-build-only-0000 DATABASE_URL=postgres://b:b@localhost:5432/b REDIS_URL=redis://localhost:6379/0 ALLOWED_HOSTS=localhost ADMIN_API_ALLOWED_NETS=127.0.0.0/8 python manage.py collectstatic --noinput
RUN useradd -u 10001 app && mkdir -p /app/media && chown -R app:app /app/media
USER app
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["api"]
