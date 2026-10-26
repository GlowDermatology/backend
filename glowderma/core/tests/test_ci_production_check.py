"""The CI "Production deploy checks" step boots the production settings (review finding, P0 fix pass 1).

P0 made eight keys mandatory under the production settings, and ``.github/workflows/ci.yml`` is lead-owned,
so the step runs here exactly as CI runs it (its ``env`` and ``run`` block, ``uv run python`` replaced by
this interpreter). The strict ``xfail`` documents that the step is red until the lead adds the keys and fails
the moment it passes, so the marker goes in the same change. The second test proves that the exports
proposed to the lead are enough.

The image build (``Dockerfile``, the ``collectstatic`` RUN line under ``config.settings.production``, the CI
``docker`` job) is lead-owned too and is checked the same way (P0 fix pass 2): it already lacked
``PUBLIC_MEDIA_BASE_URL`` on the base branch and now also needs the eight M2 keys. The proposed prefix
generates them per build (like the CI step) instead of committing build-only values, which production would
then have to refuse as committed placeholders.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from django.conf import settings

STEP_NAME = "Production deploy checks"
# The exports proposed to the lead for the step's run block (generated per run, like the M1 secrets).
PROPOSED_EXPORTS = """
export SMS_CODE_HMAC_KEY="$(openssl rand -hex 32)" LOG_PII_KEY="$(openssl rand -hex 32)"
export PAYMENT_REF_KEY="$(openssl rand -hex 32)" QR_HMAC_KEY="$(openssl rand -hex 32)"
export BTS_WEBHOOK_SECRET="$(openssl rand -hex 32)"
export CLICK_SECRET_KEY="$(openssl rand -hex 16)" PAYME_KEY="$(openssl rand -hex 16)"
export SECRETBOX_KEY="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\\n')"
"""


def ci_step() -> dict[str, Any]:
    workflow = yaml.safe_load((Path(settings.BASE_DIR) / ".github/workflows/ci.yml").read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == STEP_NAME:
                return dict(step)
    raise AssertionError(f"no {STEP_NAME!r} step in ci.yml")


def run_step(extra_exports: str = "") -> subprocess.CompletedProcess[str]:
    step = ci_step()
    script = str(step["run"]).replace("uv run python", f'"{sys.executable}"')
    if extra_exports:
        lines = script.splitlines()
        script = "\n".join([*lines[:-1], extra_exports.strip(), lines[-1]])
    keep = ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "TMPDIR")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env.update({name: str(value) for name, value in step["env"].items()})
    return subprocess.run(  # noqa: S603 - fixed interpreter and the repository's own CI script
        ["/bin/bash", "-euo", "pipefail", "-c", script],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.xfail(
    strict=True,
    reason="ci.yml is lead-owned: the lead adds the M2 production keys to the step (P0 fix pass 1 report); "
    "remove this marker in that change",
)
def test_the_ci_production_deploy_step_passes_as_committed() -> None:
    result = run_step()
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_proposed_exports_make_the_ci_production_deploy_step_pass() -> None:
    result = run_step(PROPOSED_EXPORTS)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "System check identified no issues" in result.stdout


# The prefix proposed to the lead for the Dockerfile collectstatic RUN line (POSIX sh, values made per build).
PROPOSED_DOCKER_PREFIX = (
    "export PUBLIC_MEDIA_BASE_URL=https://media.invalid/media/ "
    + " ".join(
        f"{name}=\"$(python -c 'import secrets; print(secrets.{generator})')\""
        for name, generator in (
            ("SMS_CODE_HMAC_KEY", "token_hex(32)"),
            ("LOG_PII_KEY", "token_hex(32)"),
            ("PAYMENT_REF_KEY", "token_hex(32)"),
            ("QR_HMAC_KEY", "token_hex(32)"),
            ("BTS_WEBHOOK_SECRET", "token_hex(32)"),
            ("CLICK_SECRET_KEY", "token_hex(16)"),
            ("PAYME_KEY", "token_hex(16)"),
            ("SECRETBOX_KEY", "token_urlsafe(32)"),  # base64url of 32 random bytes, unpadded
        )
    )
    + " && "
)


def dockerfile_collectstatic() -> tuple[str, str]:
    """The ``collectstatic`` RUN command of the runtime stage and that stage's ``DJANGO_SETTINGS_MODULE``."""
    text = (Path(settings.BASE_DIR) / "Dockerfile").read_text()
    module = re.search(r"^ENV .*\bDJANGO_SETTINGS_MODULE=(\S+)", text, re.MULTILINE)
    runs = [line[4:] for line in text.splitlines() if line.startswith("RUN ") and "collectstatic" in line]
    assert module is not None and len(runs) == 1, "expected one collectstatic RUN line and the settings ENV"
    return runs[0], module.group(1)


def run_dockerfile_collectstatic(prefix: str = "") -> subprocess.CompletedProcess[str]:
    command, module = dockerfile_collectstatic()
    script = (prefix + command).replace("python ", f'"{sys.executable}" ')
    script = script.replace("collectstatic --noinput", "collectstatic --noinput --dry-run")
    keep = ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "TMPDIR")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env["DJANGO_SETTINGS_MODULE"] = module
    return subprocess.run(  # noqa: S603 - fixed shell and the repository's own Dockerfile command
        ["/bin/sh", "-c", script],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.xfail(
    strict=True,
    reason="Dockerfile is lead-owned: the lead adds PUBLIC_MEDIA_BASE_URL and the M2 keys to the "
    "collectstatic RUN line (P0 fix pass 2 report); remove this marker and the failure-reason test below "
    "in that change",
)
def test_the_dockerfile_collectstatic_step_passes_as_committed() -> None:
    result = run_dockerfile_collectstatic()
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_proposed_prefix_makes_the_dockerfile_collectstatic_step_pass() -> None:
    result = run_dockerfile_collectstatic(PROPOSED_DOCKER_PREFIX)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "Pretending to copy" in result.stdout or "static file" in result.stdout


def test_the_committed_dockerfile_step_fails_on_the_missing_production_variables() -> None:
    """What the strict xfail above stands for: the image build stops at the production settings import (so the
    xfail cannot hide another failure). Deleted together with that marker."""
    result = run_dockerfile_collectstatic()
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr and "is required" in result.stderr
