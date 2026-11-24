import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings
from django.core.cache import cache
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test import Client, override_settings
from pytest_django import DjangoDbBlocker
from pytest_django.plugin import blocking_manager_key
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.models import AuthSession, Customer, StaffUser
from glowderma.accounts.tests import ops_helpers
from glowderma.accounts.tests.factories import (
    AuthSessionFactory,
    CustomerFactory,
    StaffUserFactory,
    device_headers,
    raw_device_key,
    raw_refresh,
    start_test_session,
)
from glowderma.core import qr_store
from glowderma.loyalty.models import PosDevice

POS_TEST_KEY = "gdpos_test0001_pytest-pos-key-00000000000000000"  # noqa: S105 - test-only


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    cache.clear()
    qr_store.clear_local()
    yield
    cache.clear()
    qr_store.clear_local()


@pytest.fixture(scope="session", autouse=True)
def urlconf_loaded_under_test_settings() -> None:
    """``config.urls`` decides its routes at import time (``DJANGO_ADMIN_ENABLED``, the dev routes, DEBUG
    media). Import it once under the test settings, before any test: a test that runs the deploy checks
    inside ``override_settings(<production values>)`` would otherwise import it first and leave a URLconf
    without the Django admin cached for the rest of the session ("'admin' is not a registered namespace" in
    whichever test happens to run later, review B15)."""
    import config.urls  # noqa: F401


@pytest.fixture(scope="session", autouse=True)
def static_directory() -> None:
    settings.STATIC_ROOT.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def media_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Image factories and the demo seeders write real files: keep them out of the repository's MEDIA_ROOT."""
    with override_settings(MEDIA_ROOT=tmp_path_factory.mktemp("media")):
        yield


# --- pytest-xdist: migrate once per run, clone for every worker
#
# Under `pytest -n N` pytest-django gives each worker its own test database (`<TEST NAME>_gw0`, `_gw1`, …)
# and would build every one of them from the migrations (minutes each, all at once against one server).
# Instead the first worker to need a database migrates `<TEST NAME>_template` once for this run, under a
# PostgreSQL advisory lock, and every worker copies it with `CREATE DATABASE … TEMPLATE` (seconds). Django
# then opens the copy as a kept database: its `migrate` finds nothing to apply. A plain `pytest`,
# `--reuse-db` reruns and `--nomigrations` keep pytest-django's behaviour unchanged.


def _clones_from_template(config: pytest.Config) -> bool:
    """An xdist worker whose database is built from migrations in this run (not a `--reuse-db` rerun)."""
    if getattr(config, "workerinput", None) is None:
        return False
    reuses = config.getvalue("reuse_db") and not config.getvalue("create_db")
    return not reuses and not config.getvalue("nomigrations")


# Past every explicit id a seeder inserts (all below 100 000) and far below seven digits, which
# `core.alerts` masks as a possible phone number (alert tests compare ids verbatim).
_SEQUENCE_FLOOR = 200_000


def _raise_sequences_to_floor(connection: BaseDatabaseWrapper) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT setval(format('%%I.%%I', schemaname, sequencename)::regclass, %s, false) "
            "FROM pg_sequences WHERE COALESCE(last_value, start_value) < %s",
            [_SEQUENCE_FLOOR, _SEQUENCE_FLOOR],
        )


@pytest.fixture(autouse=True)
def sequence_floor(request: pytest.FixtureRequest, django_db_blocker: DjangoDbBlocker) -> Iterator[None]:
    """Keep every sequence past the seeded ids for the next test. ``seeding.reset_sequences`` (and
    ``seed_demo``, which calls it) re-syncs sequences with ``setval``, which no rollback undoes: a
    later test of the same worker that runs a seeder and then creates a row would collide with a seeded id."""
    yield
    if "_django_db_helper" not in request.fixturenames:
        return
    from django.db import connection

    with django_db_blocker.unblock():
        _raise_sequences_to_floor(connection)


def _template_name(test_name: str) -> str:
    return f"{test_name}_template"


def _build_template(connection: BaseDatabaseWrapper, template: str) -> None:
    """Migrate a fresh ``template`` database through Django's own test-database creation."""
    test_settings = connection.settings_dict["TEST"]
    worker_name, server_name = test_settings["NAME"], connection.settings_dict["NAME"]
    test_settings["NAME"] = template
    try:
        connection.creation.create_test_db(verbosity=0, autoclobber=True)
        # Seeders insert design rows with explicit ids (customers 1…432, orders from 10001, …) and only
        # `seed_demo` re-syncs the sequences afterwards; a test that calls a seeder directly and then
        # creates a row relied on earlier tests having advanced the sequence (sequences ignore rollbacks),
        # which a fresh worker database does not give. Every sequence starts past any seeded id instead.
        _raise_sequences_to_floor(connection)
    finally:
        # CREATE DATABASE … TEMPLATE refuses a template that has open sessions.
        connection.close()
        connection.close_pool()  # type: ignore[attr-defined]  # PostgreSQL backend
        test_settings["NAME"] = worker_name
        connection.settings_dict["NAME"] = server_name
        settings.DATABASES[connection.alias]["NAME"] = server_name


def _clone_run_template(connection: BaseDatabaseWrapper, template: str, target: str, run_id: str) -> None:
    quote = connection.ops.quote_name
    with connection._nodb_cursor() as cursor:
        # One worker builds the template while the others wait; clones also take turns (a template being
        # copied must not be in use).
        cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", [template])
        try:
            cursor.execute(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s", [template]
            )
            row = cursor.fetchone()
            if row is None or row[0] != run_id:  # absent, or left over from an earlier run
                _build_template(connection, template)
                cursor.execute(f"COMMENT ON DATABASE {quote(template)} IS %s", [run_id])
            cursor.execute(f"DROP DATABASE IF EXISTS {quote(target)} WITH (FORCE)")
            cursor.execute(f"CREATE DATABASE {quote(target)} TEMPLATE {quote(template)}")
        finally:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", [template])


@pytest.fixture(scope="session")
def _xdist_database_clone(
    request: pytest.FixtureRequest,
    django_db_modify_db_settings: None,
    django_db_blocker: DjangoDbBlocker,
) -> Iterator[bool]:
    """True once this xdist worker's test database is a fresh copy of the run's migrated template."""
    config = request.config
    if not _clones_from_template(config):
        yield False
        return
    from django.db import connection, connections

    workerinput: dict[str, Any] = config.workerinput  # type: ignore[attr-defined]
    worker_db = str(connection.settings_dict["TEST"]["NAME"])  # pytest-django's `<TEST NAME>_gw<n>`
    template = _template_name(worker_db.removesuffix(f"_{workerinput['workerid']}"))
    with django_db_blocker.unblock():
        _clone_run_template(connection, template, worker_db, str(workerinput["testrunuid"]))
    yield True
    if config.getvalue("reuse_db"):  # `--reuse-db --create-db`: keep the new database for the next rerun
        return
    with django_db_blocker.unblock():
        connections.close_all()
        with connection._nodb_cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {connection.ops.quote_name(worker_db)} WITH (FORCE)")


@pytest.fixture(scope="session")
def django_db_keepdb(request: pytest.FixtureRequest, _xdist_database_clone: bool) -> bool:
    """pytest-django's ``--reuse-db``, and also true for a cloned worker database (its teardown is the clone
    fixture's): Django opens it as it is instead of dropping it and migrating anew."""
    return bool(request.config.getvalue("reuse_db")) or _xdist_database_clone


@pytest.fixture(scope="session")
def django_db_createdb(request: pytest.FixtureRequest, _xdist_database_clone: bool) -> bool:
    """pytest-django's ``--create-db``; the clone already is the new database."""
    return bool(request.config.getvalue("create_db")) and not _xdist_database_clone


def pytest_sessionfinish(session: pytest.Session) -> None:
    """The xdist controller drops the run's template once every worker has finished."""
    config = session.config
    if getattr(config, "workerinput", None) is not None or not getattr(config.option, "numprocesses", None):
        return
    from django.db import connection

    template = _template_name(str(connection.settings_dict.get("TEST", {}).get("NAME") or ""))
    with config.stash[blocking_manager_key].unblock(), connection._nodb_cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS {connection.ops.quote_name(template)} WITH (FORCE)")


def _json_client() -> APIClient:
    client = APIClient()
    client.default_format = "json"
    return client


@pytest.fixture
def staff(db: None) -> StaffUser:
    """An active staff user whose password is ``factories.TEST_PASSWORD``."""
    return StaffUserFactory.create()


@pytest.fixture
def staff_session(staff: StaffUser) -> tuple[AuthSession, str]:
    """An open session of ``staff`` together with its raw refresh token."""
    session = AuthSessionFactory.create(staff=staff)
    return session, raw_refresh(session)


@pytest.fixture
def staff_token(staff_session: tuple[AuthSession, str]) -> str:
    """A valid access JWT string for ``staff_session``."""
    access, _ = tokens.issue_access(staff_session[0])
    return access


@pytest.fixture
def staff_client(staff_token: str) -> APIClient:
    """A JSON ``APIClient`` sending ``Authorization: Bearer`` and ``X-GD-Internal-Secret`` for ``staff``.

    Named ``staff_client`` so that pytest-django's ``admin_client`` (a Django ``Client`` logged into the
    Django admin) stays available for ``/django-admin/`` pages.
    """
    client = _json_client()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {staff_token}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


@pytest.fixture
def ops_user(db: None) -> StaffUser:
    """An active ``is_staff`` console user (``ops@x.uz``, ``TEST_PASSWORD``) with a confirmed TOTP device on
    ``ops_helpers.KNOWN_TOTP_KEY``; not a superuser, not a manager."""
    return ops_helpers.ops_staff()


@pytest.fixture
def ops_client(ops_user: StaffUser) -> Client:
    """A Django test ``Client`` logged in to the ops console (``/django-admin/``) as ``ops_user`` with a
    verified 2FA session (``ops_helpers.ops_login``)."""
    client = Client()
    ops_helpers.ops_login(client, ops_user)
    return client


@pytest.fixture
def anon_client() -> APIClient:
    """A JSON ``APIClient`` without credentials."""
    return _json_client()


# --- M2 customer audience (plan §1 P0)


@pytest.fixture
def customer(db: None) -> Customer:
    """An active customer."""
    return CustomerFactory.create()


@pytest.fixture
def customer_session(customer: Customer) -> tuple[AuthSession, str]:
    """An open customer session on device ``fid-test-0001`` (ios, 1.0.0) with a device key, and its raw
    refresh token."""
    started = start_test_session(customer)
    return started.session, started.raw_refresh


@pytest.fixture
def customer_token(customer_session: tuple[AuthSession, str]) -> str:
    """A valid customer access JWT for ``customer_session``."""
    access, _ = tokens.issue_access(customer_session[0])
    return access


@pytest.fixture
def customer_client(customer_session: tuple[AuthSession, str], customer_token: str) -> APIClient:
    """A JSON ``APIClient`` with the customer bearer, the session's device headers and its device key."""
    session = customer_session[0]
    if session.device is None:  # pragma: no cover - start_customer_session always binds a device
        raise RuntimeError("customer session without a device")
    client = _json_client()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {customer_token}",
        **device_headers(key=raw_device_key(session.device)),
    )
    return client


@pytest.fixture
def public_client() -> APIClient:
    """An anonymous app client: device headers of another install, no bearer."""
    client = _json_client()
    client.credentials(**device_headers(fid="fid-public-0001", platform="android"))
    return client


@pytest.fixture
def pos_key() -> str:
    """The raw API key of ``pos_device`` (format ``gdpos_<8-char prefix>_<32 base64url chars>``)."""
    return POS_TEST_KEY


@pytest.fixture
def pos_device(db: None, pos_key: str) -> PosDevice:
    """An active POS device whose key hash matches ``pos_key``."""
    return PosDevice.objects.create(
        name="Kassa test",
        key_prefix=pos_key.split("_")[1],
        key_hash=hashlib.sha256(pos_key.encode()).hexdigest(),
    )


@pytest.fixture
def pos_client(pos_device: PosDevice, pos_key: str) -> APIClient:
    """A JSON ``APIClient`` sending ``Authorization: Api-Key <key>`` for ``pos_device``."""
    client = _json_client()
    client.credentials(HTTP_AUTHORIZATION=f"Api-Key {pos_key}")
    return client
