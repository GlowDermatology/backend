#!/usr/bin/env python3
"""Behaviour probe of deploy/Caddyfile: runs the real file in a real Caddy in front of a stub upstream.

Usage: python3 deploy/edge_probe.py --caddy /path/to/caddy        (exit 0 = every check passed)

The Caddyfile is run as committed except for what a local run needs: plain HTTP on a free port
(auto_https off, no admin endpoint) and test host names (site.test, www.site.test, admin.test, api.test). The
upstreams and the media root come from the variables the Caddyfile already reads (API_UPSTREAM,
FRONTEND_UPSTREAM, MEDIA_ROOT).

Checks: the provider callbacks and POS reach Django with the query string intact; the private paths answer 404
at the edge; the body limits; X-Forwarded-For is overwritten; no Server/Via header and HSTS on every host
(www included); a client trickling its body never reaches the upstream; the server timeouts; and no secret or
personal value from a query string, a Referer or a token header appears in the access log (stdout) or the
error log (stderr), including the error line of a 502 while the upstream is down. CI runs it with the binary
of the pinned caddy image (.github/workflows/ci.yml); glowderma/core/tests/test_deploy_stack.py runs it when a
caddy binary is available (CADDY_BIN or PATH).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CADDYFILE = Path(__file__).resolve().with_name("Caddyfile")
HOSTS = {"SITE_DOMAIN": "site.test", "ADMIN_DOMAIN": "admin.test", "API_DOMAIN": "api.test"}
# Values that must never appear in a log line (each is sent once, in a query string or a header).
SECRETS = {
    "bts_token": "probe-bts-token-0123456789abcdef",
    "bts_header": "probe-bts-header-0123456789abcdef",
    "slow_token": "probe-slow-token-0123456789abcdef",
    "down_token": "probe-down-token-0123456789abcdef",
    "down_header": "probe-down-header-0123456789abcdef",
    "internal": "probe-internal-secret-0123456789abcdef",
    "phone": "998901234567",
    "referer_phone": "998907654321",
    "oauth_code": "probe-oauth-code-0123456789",
    "oauth_state": "probe-oauth-state-0123456789",
    "down_phone": "998909999999",
}
MIB = 1024 * 1024


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body_length: int


@dataclass
class Upstream:
    requests: list[Seen] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def paths(self) -> list[str]:
        with self.lock:
            return [seen.path for seen in self.requests]


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def stub_server(upstream: Upstream) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def answer(self) -> None:
            # Recorded on arrival, before the body is read: a request forwarded while its body is still
            # trickling in shows up here at once.
            seen = Seen(self.command, self.path, dict(self.headers.items()), -1)
            with upstream.lock:
                upstream.requests.append(seen)
            length = int(self.headers.get("Content-Length") or 0)
            seen.body_length = len(self.rfile.read(length)) if length else 0
            payload = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Server", "stub")
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = answer

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
            return

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request: object, client_address: object) -> None:
            return  # the edge cut a too-large body short (413): nothing to report

    server = Server(("127.0.0.1", free_port()), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def probe_caddyfile(port: int) -> str:
    """The committed Caddyfile, served over plain HTTP on ``port`` with test host names."""
    text = CADDYFILE.read_text(encoding="utf-8")
    text, count = re.subn(
        r"^\{\n\temail \{\$ACME_EMAIL\}\n",
        f"{{\n\temail {{$ACME_EMAIL}}\n\tadmin off\n\tauto_https off\n\thttp_port {port}\n",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit("probe: the global options block no longer starts with `email {$ACME_EMAIL}`")
    text, sites = re.subn(r"^(\S*\{\$[A-Z_]+_DOMAIN\}) \{$", r"http://\1 {", text, flags=re.MULTILINE)
    if sites != 4:
        raise SystemExit(f"probe: expected 4 site blocks, found {sites}")
    return text


@contextmanager
def running_caddy(caddy: str, workdir: Path, upstream_port: int) -> Iterator[tuple[int, Path, Path]]:
    port = free_port()
    config = workdir / "Caddyfile"
    config.write_text(probe_caddyfile(port), encoding="utf-8")
    media = workdir / "media"
    media.mkdir()
    (media / "a.txt").write_text("media", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workdir),
        "XDG_DATA_HOME": str(workdir / "xdg"),
        "XDG_CONFIG_HOME": str(workdir / "xdg"),
        "ACME_EMAIL": "probe@example.invalid",
        "API_UPSTREAM": f"127.0.0.1:{upstream_port}",
        "FRONTEND_UPSTREAM": f"127.0.0.1:{upstream_port}",
        "MEDIA_ROOT": str(media),
        **HOSTS,
    }
    out, err = workdir / "caddy.out", workdir / "caddy.err"
    with out.open("wb") as stdout, err.open("wb") as stderr:
        process = subprocess.Popen(  # noqa: S603 - the caddy binary the caller names, fixed arguments
            [caddy, "run", "--config", str(config), "--adapter", "caddyfile"],
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise SystemExit("probe: caddy exited:\n" + err.read_text(encoding="utf-8")[-2000:])
                try:
                    with closing(socket.create_connection(("127.0.0.1", port), timeout=0.5)):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise SystemExit("probe: caddy did not listen within 15 s") from None
                    time.sleep(0.1)
            yield port, out, err
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@dataclass
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes


def request(
    port: int,
    host: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Answer:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(method, path, body=body, headers={"Host": host, **(headers or {})})
        response = connection.getresponse()
        return Answer(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
    finally:
        connection.close()


class Probe:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, condition: bool, message: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append(message)


def adapted_servers(caddy: str, workdir: Path) -> list[dict[str, object]]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workdir),
        "XDG_DATA_HOME": str(workdir / "xdg"),
        "XDG_CONFIG_HOME": str(workdir / "xdg"),
        "ACME_EMAIL": "probe@example.invalid",
        "SITE_DOMAIN": "example.com",
        "ADMIN_DOMAIN": "admin.example.com",
        "API_DOMAIN": "api.example.com",
    }
    result = subprocess.run(  # noqa: S603 - the caddy binary the caller names, fixed arguments
        [caddy, "adapt", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout.splitlines()[0])
    servers: dict[str, dict[str, object]] = config["apps"]["http"]["servers"]
    return list(servers.values())


def slow_post(port: int, host: str, path: str, upstream: Upstream) -> list[str]:
    """Send the headers and 10 of 1000 body bytes and idle for a second: the paths the upstream received
    meanwhile (the connection is still open, the body incomplete)."""
    before = len(upstream.paths())
    with closing(socket.create_connection(("127.0.0.1", port), timeout=5)) as sock:
        head = (
            f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
            "Content-Length: 1000\r\n\r\n"
        )
        sock.sendall(head.encode() + b"{" + b" " * 9)
        time.sleep(1.0)
        return upstream.paths()[before:]


def run(caddy: str) -> Probe:
    probe = Probe()
    upstream = Upstream()
    server = stub_server(upstream)
    upstream_port = server.server_address[1]
    api, admin, site, www = HOSTS["API_DOMAIN"], HOSTS["ADMIN_DOMAIN"], HOSTS["SITE_DOMAIN"], "www.site.test"
    with tempfile.TemporaryDirectory(prefix="edge-probe-") as tmp:
        workdir = Path(tmp)
        for server_config in adapted_servers(caddy, workdir):
            probe.check(
                server_config.get("read_timeout") == 60_000_000_000
                and server_config.get("read_header_timeout") == 10_000_000_000,
                "server timeouts: read_body 60s / read_header 10s expected, got "
                f"{server_config.get('read_timeout')} / {server_config.get('read_header_timeout')} ns",
            )
        with running_caddy(caddy, workdir, int(upstream_port)) as (port, out, err):
            # Provider callbacks and POS reach Django, query string intact.
            callbacks = [
                "/api/v1/webhooks/click/prepare/",
                "/api/v1/webhooks/click/complete/",
                "/api/v1/webhooks/payme/",
                f"/api/v1/webhooks/bts/?token={SECRETS['bts_token']}",
                "/api/v1/pos/cashback/resolve-qr/",
            ]
            for path in callbacks:
                answer = request(
                    port,
                    api,
                    "POST",
                    path,
                    body=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-GD-Webhook-Token": SECRETS["bts_header"],
                        "X-GD-Internal-Secret": SECRETS["internal"],
                        "X-Forwarded-For": "203.0.113.9",
                    },
                )
                probe.check(
                    answer.status == 200, f"POST {path}: expected 200 from the upstream, got {answer.status}"
                )
                probe.check(path in upstream.paths(), f"POST {path}: the upstream did not see it intact")
            with upstream.lock:
                forwarded = {seen.headers.get("X-Forwarded-For") for seen in upstream.requests}
            probe.check(
                forwarded == {"127.0.0.1"}, f"X-Forwarded-For must be the TCP peer only, got {forwarded}"
            )
            # Private paths never leave the edge.
            for path in (
                "/api/v1/admin/auth/me/",
                "/api/v1/schema/",
                "/api/v1/docs/",
                "/api/v1/dev/payments/1/simulate/",
                "/django-admin/",
            ):
                answer = request(port, api, "GET", path)
                probe.check(
                    answer.status == 404, f"GET {path}: expected 404 at the edge, got {answer.status}"
                )
                probe.check(path not in upstream.paths(), f"GET {path} reached the upstream")
            # Body limits: 1 MiB on callbacks and POS, 6 MiB elsewhere on the API host.
            big = request(port, api, "POST", "/api/v1/webhooks/payme/", body=b"x" * (MIB + 1))
            probe.check(big.status == 413, f"webhook body of 1 MiB + 1: expected 413, got {big.status}")
            upload = request(
                port, api, "POST", "/api/v1/customer/me/avatar/", body=b"x" * (5 * MIB + MIB // 2)
            )
            probe.check(upload.status == 200, f"customer body of 5.5 MiB: expected 200, got {upload.status}")
            too_big = request(port, api, "POST", "/api/v1/customer/me/avatar/", body=b"x" * (6 * MIB + 1))
            probe.check(
                too_big.status == 413, f"customer body of 6 MiB + 1: expected 413, got {too_big.status}"
            )
            # Response headers on every host.
            for host, path in ((api, "/api/v1/health/"), (admin, "/admin"), (site, "/"), (www, "/x?y=1")):
                answer = request(port, host, "GET", path)
                probe.check("server" not in answer.headers, f"{host}: Server header present")
                probe.check("via" not in answer.headers, f"{host}: Via header present")
                probe.check(
                    answer.headers.get("strict-transport-security") == "max-age=31536000",
                    f"{host}: Strict-Transport-Security missing",
                )
            redirect = request(port, www, "GET", "/x?y=1")
            probe.check(
                redirect.status == 308 and redirect.headers.get("location") == f"https://{site}/x?y=1",
                f"www: expected 308 to the apex, got {redirect.status} {redirect.headers.get('location')}",
            )
            media = request(port, api, "GET", "/media/a.txt")
            probe.check(
                media.status == 200 and media.headers.get("x-content-type-options") == "nosniff",
                f"/media/: expected 200 with nosniff, got {media.status}",
            )
            # Personal data and OAuth values in admin URLs and their Referer.
            request(
                port,
                admin,
                "GET",
                f"/admin/orders?q={SECRETS['phone']}",
                headers={"Referer": f"http://{admin}/admin/customers?q={SECRETS['referer_phone']}"},
            )
            request(
                port,
                admin,
                "GET",
                f"/admin/auth/oauth/google/callback?code={SECRETS['oauth_code']}&state={SECRETS['oauth_state']}",
            )
            # A trickling client: the edge holds the request, Django never sees it.
            for path in (
                f"/api/v1/webhooks/bts/?token={SECRETS['slow_token']}",
                "/api/v1/customer/auth/login/",
            ):
                arrived = slow_post(port, api, path, upstream)
                probe.check(
                    not arrived,
                    f"slow POST {path.split('?')[0]}: reached the upstream before its body was complete",
                )
            # The upstream goes away (api restarting): the 502 error line must be redacted as well.
            server.shutdown()
            server.server_close()
            down = request(
                port,
                api,
                "POST",
                f"/api/v1/webhooks/bts/?token={SECRETS['down_token']}",
                body=b"{}",
                headers={"X-GD-Webhook-Token": SECRETS["down_header"]},
            )
            probe.check(down.status == 502, f"upstream down: expected 502, got {down.status}")
            request(port, admin, "GET", f"/admin/orders?q={SECRETS['down_phone']}")
            time.sleep(0.5)
        logs = {
            "access log (stdout)": out.read_text(encoding="utf-8"),
            "error log (stderr)": err.read_text("utf-8"),
        }
    for name, text in logs.items():
        for label, value in SECRETS.items():
            probe.check(value not in text, f"{name} contains the {label} value")
    probe.check("?REDACTED" in logs["access log (stdout)"], "access log: no redacted query string found")
    probe.check(
        "/api/v1/webhooks/bts/?REDACTED" in logs["error log (stderr)"],
        "error log: the 502 of the BTS webhook was not logged (redacted)",
    )
    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--caddy", default=os.environ.get("CADDY_BIN", "caddy"), help="caddy binary")
    args = parser.parse_args(argv)
    probe = run(args.caddy)
    for failure in probe.failures:
        print(f"FAIL {failure}")
    print(f"edge probe: {probe.passed} checks passed, {len(probe.failures)} failed")
    return 1 if probe.failures else 0


if __name__ == "__main__":
    sys.exit(main())
