"""The CI "Production deploy checks" step and the Dockerfile collectstatic step boot production settings.

Both run here exactly as committed (their env and commands, the interpreter swapped for this one), so a new
required production setting that the CI step or the image build does not provide fails this suite instead of
CI or ``docker build``. The ``PROPOSED_*`` tests keep the generated-per-run values honest on their own.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def test_the_dockerfile_collectstatic_step_passes_as_committed() -> None:
    result = run_dockerfile_collectstatic()
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_proposed_prefix_makes_the_dockerfile_collectstatic_step_pass() -> None:
    result = run_dockerfile_collectstatic(PROPOSED_DOCKER_PREFIX)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "Pretending to copy" in result.stdout or "static file" in result.stdout
