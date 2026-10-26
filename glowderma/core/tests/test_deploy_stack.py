"""The production stack beyond the settings: compose.prod.yaml, the deploy/ scripts, the CI steps that check
them and the runbook commands that once were wrong.

Everything runs offline. The checks that need an external binary skip without it: ``docker`` (the Compose CLI;
no daemon), ``caddy`` (``CADDY_BIN`` or PATH: the edge probe) and ``psql`` with a superuser on the configured
server (the CI migrations-as-``glowderma_app`` step).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from django.conf import settings
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(settings.BASE_DIR)
COMPOSE_FILE = ROOT / "compose.prod.yaml"
COMPOSE: dict[str, Any] = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
CI: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
DEPLOY_MD = (ROOT / "docs/ops/deploy.md").read_text(encoding="utf-8")
OPS_DOCS = {path.name: path.read_text(encoding="utf-8") for path in (ROOT / "docs/ops").glob("*.md")}
KEEP_ENV = ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "TMPDIR")


def base_env(**extra: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in KEEP_ENV if name in os.environ}
    env.update(extra)
    return env


def ci_step(name: str) -> dict[str, Any]:
    for job in CI["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return dict(step)
    raise AssertionError(f"no {name!r} step in ci.yml")


def section(title_prefix: str) -> str:
    """The text of the deploy.md section whose heading starts with ``## <title_prefix>``."""
    match = re.search(rf"^## {re.escape(title_prefix)}.*?(?=^## |\Z)", DEPLOY_MD, re.MULTILINE | re.DOTALL)
    assert match, title_prefix
    return match.group(0)


# --- compose.prod.yaml --------------------------------------------------------------------------------------


def networks_of(service: str) -> set[str]:
    networks = SERVICES[service]["networks"]
    return set(networks) if isinstance(networks, (list, dict)) else set()


def test_the_data_stores_are_unreachable_from_the_internet_facing_containers() -> None:
    assert COMPOSE["networks"]["back"].get("internal") is True
    assert networks_of("postgres") == networks_of("redis") == {"back"}
    assert "back" not in networks_of("edge") | networks_of("frontend")
    assert networks_of("api") == {"front", "back"}
    assert networks_of("worker") == {"back", "egress"}  # outbound HTTPS to the providers
    assert networks_of("beat") == {"back"}
    assert SERVICES["edge"]["networks"]["front"]["ipv4_address"] == "172.30.0.10"
    assert SERVICES["frontend"]["networks"]["front"]["ipv4_address"] == "172.30.0.20"


def test_redis_requires_a_password_that_never_appears_in_a_process_list() -> None:
    redis = SERVICES["redis"]
    command = "\n".join(redis["command"])
    assert "--requirepass" not in command
    assert "redis-server -" in command and "requirepass $$REDISCLI_AUTH" in command
    assert "--maxmemory" in command
    assert redis["environment"]["REDISCLI_AUTH"] == "${REDIS_PASSWORD}"
    backend = SERVICES["api"]["environment"]
    assert backend["REDIS_URL"].startswith("redis://:${REDIS_PASSWORD:?")
    assert backend["CELERY_BROKER_URL"] == "redis://:${REDIS_PASSWORD}@redis:6379/1"


def test_the_application_connects_as_the_non_superuser_role() -> None:
    assert SERVICES["api"]["environment"]["DATABASE_URL"].startswith(
        "postgres://glowderma_app:${POSTGRES_APP_PASSWORD:?"
    )
    postgres = SERVICES["postgres"]
    assert "./deploy/postgres:/docker-entrypoint-initdb.d:ro" in postgres["volumes"]
    assert postgres["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert postgres["environment"]["POSTGRES_APP_PASSWORD"].startswith("${POSTGRES_APP_PASSWORD:?")
    assert os.access(ROOT / "deploy/postgres/10-app-role.sh", os.X_OK)
    assert not list((ROOT / "deploy/postgres").glob("*.sql"))  # the image would run it without the password
    # Over TCP: the image's temporary init server listens on the socket only.
    assert "-h" in postgres["healthcheck"]["test"]


def test_image_tags_come_from_env_and_are_required() -> None:
    assert SERVICES["api"]["image"].startswith("glowderma-backend:${GIT_SHA:?")
    assert SERVICES["frontend"]["image"].startswith("glowderma-frontend:${FRONTEND_GIT_SHA:?")
    for name in ("postgres", "redis", "edge"):
        assert re.search(r":\d+\.\d+", SERVICES[name]["image"]), f"{name}: pin a minor version"
    services = CI["jobs"]["checks"]["services"]
    assert services["postgres"]["image"].split()[0] == SERVICES["postgres"]["image"]
    assert services["redis"]["image"] == SERVICES["redis"]["image"]


def test_every_long_running_container_is_bounded() -> None:
    for name in ("api", "worker", "beat", "frontend", "edge"):
        assert SERVICES[name].get("mem_limit"), name
        assert SERVICES[name].get("pids_limit"), name
    for name in ("postgres", "redis"):
        assert SERVICES[name]["oom_score_adj"] < 0, name


def test_the_edge_sees_a_pulled_caddyfile_and_publishes_ipv4_only() -> None:
    edge = SERVICES["edge"]
    assert "./deploy:/etc/caddy:ro" in edge["volumes"]
    assert not [volume for volume in edge["volumes"] if "Caddyfile" in volume]
    assert all(port.startswith("0.0.0.0:") for port in edge["ports"])


def test_the_backend_containers_get_no_infrastructure_password() -> None:
    environment = SERVICES["worker"]["environment"]
    for name in ("POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "REDIS_PASSWORD"):
        assert environment[name] == ""


def docker_compose() -> list[str] | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    probe = subprocess.run([docker, "compose", "version"], capture_output=True, check=False)  # noqa: S603
    return [docker, "compose"] if probe.returncode == 0 else None


def test_the_ci_compose_step_passes_as_committed(tmp_path: Path) -> None:
    """The "Production compose file resolves" step, run in a copy of the files it reads (it writes .env)."""
    compose = docker_compose()
    if compose is None:
        pytest.skip("no docker compose CLI")
    shutil.copy(COMPOSE_FILE, tmp_path / "compose.prod.yaml")
    shutil.copytree(ROOT / "deploy", tmp_path / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(  # noqa: S603 - bash and the repository's own CI script
        ["/bin/bash", "-euo", "pipefail", "-c", str(ci_step("Production compose file resolves")["run"])],
        cwd=tmp_path,
        env=base_env(GITHUB_SHA="0123456789abcdef"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert not (tmp_path / ".env").exists()


# --- deploy/backup.sh and its cron entry --------------------------------------------------------------------

STUB_DOCKER = r"""#!/usr/bin/env bash
# docker compose -f <file> --project-directory <dir> exec -T <service> <command...>
args=" $* "
case "$args" in
*" exec -T postgres pg_dump "*)
	[ -n "${STUB_FAIL_DUMP:-}" ] && { printf 'PGDMP-partial'; echo "pg_dump: error" >&2; exit 1; }
	printf 'PGDMP-stub-archive'
	;;
*" exec -T postgres pg_restore "*)
	data=$(cat)
	[ -z "${STUB_FAIL_VERIFY:-}" ] && [ "${data#PGDMP}" != "$data" ]
	;;
*" exec -T edge tar "*)
	[ -n "${STUB_FAIL_MEDIA:-}" ] && exit 1
	tar -cf - -C "$STUB_MEDIA" .
	;;
*)
	echo "unexpected docker call:$args" >&2
	exit 2
	;;
esac
"""


def run_backup(tmp_path: Path, label: str, **flags: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(STUB_DOCKER, encoding="utf-8")
        (bin_dir / "docker").chmod(0o755)
        media = tmp_path / "media"
        media.mkdir()
        (media / "avatar.webp").write_bytes(b"image")
    env = base_env(
        PATH=f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        GLOWDERMA_STACK_DIR=str(ROOT),
        GLOWDERMA_BACKUP_DIR=str(tmp_path / "backups"),
        STUB_MEDIA=str(tmp_path / "media"),
        **flags,
    )
    return subprocess.run(  # noqa: S603 - the repository's own backup script against a stub docker
        ["/bin/bash", str(ROOT / "deploy/backup.sh"), label],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def backups(tmp_path: Path) -> list[str]:
    return sorted(path.name for path in (tmp_path / "backups").iterdir())


def test_a_nightly_backup_writes_a_verified_dump_and_media_tarball(tmp_path: Path) -> None:
    result = run_backup(tmp_path, "nightly")
    assert result.returncode == 0, result.stderr
    names = backups(tmp_path)
    assert len(names) == 2 and names[0].startswith("db-nightly-") and names[1].startswith("media-")
    directory = tmp_path / "backups"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in names:
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert (directory / names[0]).read_bytes() == b"PGDMP-stub-archive"
    with tarfile.open(directory / names[1]) as archive:
        assert "./avatar.webp" in archive.getnames()


def test_a_pre_upgrade_backup_writes_the_dump_only(tmp_path: Path) -> None:
    result = run_backup(tmp_path, "pre-upgrade")
    assert result.returncode == 0, result.stderr
    [name] = backups(tmp_path)
    assert name.startswith("db-pre-upgrade-") and name.endswith(".dump")


@pytest.mark.parametrize("flag", ["STUB_FAIL_DUMP", "STUB_FAIL_VERIFY"])
def test_a_failed_or_unreadable_dump_leaves_no_file_and_fails(tmp_path: Path, flag: str) -> None:
    result = run_backup(tmp_path, "pre-upgrade", **{flag: "1"})
    assert result.returncode == 1
    assert "FAILED" in result.stderr
    assert backups(tmp_path) == []


def test_a_failed_media_backup_fails_and_prunes_nothing(tmp_path: Path) -> None:
    (tmp_path / "backups").mkdir(mode=0o700)
    old = tmp_path / "backups" / "db-nightly-2026-01-01-021500.dump"
    old.write_bytes(b"PGDMP-old")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))
    result = run_backup(tmp_path, "nightly", STUB_FAIL_MEDIA="1")
    assert result.returncode == 1 and "media" in result.stderr
    assert old.exists()
    assert not [name for name in backups(tmp_path) if name.endswith(".tmp") or name.startswith("media-")]


def test_a_successful_nightly_run_prunes_old_nightly_files_only(tmp_path: Path) -> None:
    (tmp_path / "backups").mkdir(mode=0o700)
    stale = time.time() - 30 * 86400
    old_nightly = tmp_path / "backups" / "db-nightly-2026-01-01-021500.dump"
    old_upgrade = tmp_path / "backups" / "db-pre-upgrade-2026-01-01-101500.dump"
    for path in (old_nightly, old_upgrade):
        path.write_bytes(b"PGDMP-old")
        os.utime(path, (stale, stale))
    result = run_backup(tmp_path, "nightly")
    assert result.returncode == 0, result.stderr
    assert not old_nightly.exists()
    assert old_upgrade.exists()


def test_an_unknown_label_is_refused(tmp_path: Path) -> None:
    assert run_backup(tmp_path, "weekly").returncode == 2


def test_the_cron_entry_is_one_line_per_job() -> None:
    """Cron has no line continuation; % is special. The entry runs the committed script."""
    text = (ROOT / "deploy/glowderma-backup.cron").read_text(encoding="utf-8")
    assert text.endswith("\n")
    jobs = [
        line for line in text.splitlines() if line and not line.startswith("#") and "=" not in line.split()[0]
    ]
    assert jobs == [
        "15 21 * * * root /srv/glowderma/backend/deploy/backup.sh nightly"
        " >>/var/log/glowderma-backup.log 2>&1"
    ]
    for line in text.splitlines():
        assert not line.rstrip().endswith("\\"), line
        assert line.startswith("#") or "%" not in line, line
    assert os.access(ROOT / "deploy/backup.sh", os.X_OK)


def test_no_runbook_cron_entry_is_split_over_lines() -> None:
    for name, text in OPS_DOCS.items():
        for line in text.splitlines():
            if re.match(r"^#?\s*[\d*/,-]+ [\d*/,-]+ [\d*/,-]+ [\d*/,-]+ [\d*/,-]+ \w+ ", line):
                assert not line.rstrip().endswith("\\"), f"{name}: {line}"


# --- deploy/pin-release.sh ----------------------------------------------------------------------------------


def pin(tmp_path: Path, *tags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the repository's own script
        ["/bin/sh", str(ROOT / "deploy/pin-release.sh"), *tags],
        cwd=tmp_path,
        env=base_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_pin_release_writes_the_tags_into_env_and_keeps_the_rest(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=x\nGIT_SHA=old\nAPI_DOMAIN=api.example.uz\nFRONTEND_GIT_SHA=old\n")
    env_file.chmod(0o600)
    assert pin(tmp_path, "abc1234", "def5678").returncode == 0
    assert env_file.read_text() == (
        "SECRET_KEY=x\nAPI_DOMAIN=api.example.uz\nGIT_SHA=abc1234\nFRONTEND_GIT_SHA=def5678\n"
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert [path.name for path in tmp_path.iterdir()] == [".env"]


@pytest.mark.parametrize("tags", [("bad tag", "x"), ("abc", "$(id)"), ("only-one",)])
def test_pin_release_refuses_bad_arguments_and_leaves_env_alone(
    tmp_path: Path, tags: tuple[str, ...]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GIT_SHA=keep\n")
    assert pin(tmp_path, *tags).returncode == 2
    assert env_file.read_text() == "GIT_SHA=keep\n"


def test_pin_release_needs_the_env_file(tmp_path: Path) -> None:
    assert pin(tmp_path, "abc", "def").returncode == 1


# --- the runbook --------------------------------------------------------------------------------------------


def test_the_runbook_never_migrates_with_the_running_container() -> None:
    """``exec api … migrate`` runs the previous release's code during an upgrade; ``run --rm api migrate``
    the new one."""
    for name, text in OPS_DOCS.items():
        assert not re.search(r"exec\s+api\s+python\s+manage\.py\s+migrate", text), name


def test_the_runbook_uses_psql_in_the_postgres_container() -> None:
    for name, text in OPS_DOCS.items():
        assert "dbshell" not in text, name  # the backend image has no psql


def test_every_resolved_outbox_command_shows_its_dry_run() -> None:
    for name, text in OPS_DOCS.items():
        for span in re.findall(r"`([^`]*requeue_outbox[^`]*--resolved[^`]*)`", text):
            assert "--dry-run" in span, f"{name}: {span}"


def test_upgrades_back_up_first_stop_the_writers_and_reload_the_edge() -> None:
    upgrades = section("4. Upgrades")
    prepare = upgrades.split("<<'PREPARE'", 1)[1].split("PREPARE\n", 1)[0]
    assert prepare.lstrip().startswith("deploy/backup.sh pre-upgrade")
    assert "deploy/pin-release.sh" in prepare
    release = upgrades.split("<<'RELEASE'", 1)[1].split("RELEASE\n", 1)[0]
    commands = [line.split("#")[0].strip() for line in release.strip().splitlines()]
    assert commands == [
        "docker compose -f compose.prod.yaml stop api worker beat",
        "docker compose -f compose.prod.yaml run --rm api migrate",
        "docker compose -f compose.prod.yaml up -d --wait",
        "docker compose -f compose.prod.yaml exec edge caddy reload --config /etc/caddy/Caddyfile"
        " --adapter caddyfile",
    ]
    assert upgrades.count("bash -euo pipefail <<") == 2


def test_the_m1_upgrade_recreates_the_stack_and_hands_the_database_over() -> None:
    m1 = section("5. Upgrading an M1")
    switch = m1.split("<<'M3'", 1)[1].split("M3\n", 1)[0]
    assert "compose.prod.yaml down" in switch and "docker network rm glowderma_internal" in switch
    assert (
        switch.index("10-app-role.sh") < switch.index("run --rm api migrate") < switch.rindex("up -d --wait")
    )


def test_restores_save_the_current_database_and_run_in_one_transaction() -> None:
    restore = section("11. Backups").split("<<'RESTORE'", 1)[1].split("RESTORE\n", 1)[0]
    assert "deploy/backup.sh pre-restore" in restore
    assert "--exit-on-error" in restore and "--single-transaction" in restore
    assert restore.index("pg_restore") < restore.index("up -d --wait")
    assert "dump=/srv/backups/" in restore  # an absolute path: the block runs in the backend checkout
    assert "deploy/glowderma-backup.cron" in section("11. Backups")


def test_dns_is_ipv4_only() -> None:
    prerequisites = section("2. Prerequisites")
    assert "**`A` records only** (no `AAAA`)" in prerequisites


# --- the edge probe (needs a caddy binary) ------------------------------------------------------------------


def test_the_edge_probe_passes() -> None:
    caddy = os.environ.get("CADDY_BIN") or shutil.which("caddy")
    if not caddy:
        pytest.skip(
            "no caddy binary (set CADDY_BIN); CI runs deploy/edge_probe.py with the pinned image's binary"
        )
    result = subprocess.run(  # noqa: S603 - this interpreter and the repository's own probe
        [sys.executable, str(ROOT / "deploy/edge_probe.py"), "--caddy", caddy],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert "0 failed" in result.stdout


# --- the CI step that migrates as glowderma_app (needs psql and a superuser) --------------------------------

APP_ROLE_STEP = "Migrations run as the non-superuser application role"


def test_the_ci_app_role_step_passes_as_committed() -> None:
    """Runs the step's script with its own environment (the CI service = the development server), on a
    database name of its own; the cluster-wide role glowderma_app stays (the script is idempotent). An
    advisory lock serialises parallel runs, which would otherwise reset each other's role password."""
    step = ci_step(APP_ROLE_STEP)
    env = {name: str(value) for name, value in step["env"].items()}
    if shutil.which("psql") is None:
        pytest.skip("no psql")
    database = f"gd_app_role_{os.getpid()}"
    script = str(step["run"]).replace("gd_app_role", database).replace("uv run python", f'"{sys.executable}"')
    env["POSTGRES_DB"] = database
    conninfo = make_conninfo(
        host=env["PGHOST"],
        port=env["PGPORT"],
        user=env["PGUSER"],
        password=env["PGPASSWORD"],
        dbname="postgres",
        connect_timeout=3,
    )
    try:
        lock = psycopg.connect(conninfo, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("the CI step's database server is not reachable")
    with lock:
        row = lock.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        if not (row and row[0]):
            pytest.skip("the CI step needs a superuser")
        lock.execute("SELECT pg_advisory_lock(hashtext('glowderma-ci-app-role'))")
        try:
            result = subprocess.run(  # noqa: S603 - bash and the repository's own CI script
                ["/bin/bash", "-euo", "pipefail", "-c", script],
                cwd=ROOT,
                env=base_env(**env),
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        finally:
            lock.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
            lock.execute("SELECT pg_advisory_unlock(hashtext('glowderma-ci-app-role'))")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "Applying" in result.stdout or "No migrations to apply" in result.stdout
