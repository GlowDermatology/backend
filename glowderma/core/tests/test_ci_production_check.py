"""The CI "Production deploy checks" step boots the production settings (review finding, P0 fix pass 1).

P0 made eight keys mandatory under the production settings, and ``.github/workflows/ci.yml`` is lead-owned,
so the step runs here exactly as CI runs it (its ``env`` and ``run`` block, ``uv run python`` replaced by
this interpreter). The strict ``xfail`` documents that the step is red until the lead adds the keys and fails
the moment it passes, so the marker goes in the same change. The second test proves that the exports
proposed to the lead are enough.
"""

import os
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
