#!/usr/bin/env python3
"""Behaviour probe of deploy/Caddyfile: runs the real file in a real Caddy in front of a stub upstream.

Usage: python3 deploy/edge_probe.py --caddy /path/to/caddy        (exit 0 = every check passed)

The Caddyfile is run as committed except for what a local run needs: plain HTTP on a free port
(auto_https off, no admin endpoint) and test host names (site.test, www.site.test, admin.test, api.test,
ops.test). The upstreams and the media root come from the variables the Caddyfile already reads (API_UPSTREAM,
FRONTEND_UPSTREAM, MEDIA_ROOT); the ops console's allow-list (OPS_ALLOWED_IPS) is 127.0.0.1 only, and a
request from 127.0.0.2 (another loopback address, Linux) plays a browser outside it.

Checks: the provider callbacks and POS reach Django with the query string intact; the private paths answer 404
at the edge; the body limits; X-Forwarded-For is overwritten; no Server/Via header and HSTS on every host
(www included); the ops console host (an allowed address reaches the login page with the CSP and the other
security headers and the /media/ previews, / redirects to /django-admin/, any other path is an empty 404, an
address outside the allow-list gets an empty 404 for everything and never reaches Django); a client
trickling its body never
reaches the upstream; the server timeouts; and no secret or
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
HOSTS = {
    "SITE_DOMAIN": "site.test",
    "ADMIN_DOMAIN": "admin.test",
    "API_DOMAIN": "api.test",
    "OPS_DOMAIN": "ops.test",
}
OPS_ALLOWED = "127.0.0.1/32"
OUTSIDE_ADDRESS = "127.0.0.2"  # a loopback source address outside OPS_ALLOWED
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
    "ops_phone": "998905555555",
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
            # Django's own value: the ops host must replace it with DENY (one header, not two).
            self.send_header("X-Frame-Options", "SAMEORIGIN")
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
    if sites != 5:
        raise SystemExit(f"probe: expected 5 site blocks, found {sites}")
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
        "OPS_ALLOWED_IPS": OPS_ALLOWED,
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
    all_headers: list[tuple[str, str]] = field(default_factory=list)

    def values(self, name: str) -> list[str]:
        return [value for key, value in self.all_headers if key.lower() == name.lower()]


def request(
    port: int,
    host: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    source: str | None = None,
) -> Answer:
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=20, source_address=(source, 0) if source else None
    )
    try:
        connection.request(method, path, body=body, headers={"Host": host, **(headers or {})})
        response = connection.getresponse()
        raw = response.getheaders()
        return Answer(response.status, {k.lower(): v for k, v in raw}, response.read(), raw)
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
        "OPS_DOMAIN": "ops.example.com",
        "OPS_ALLOWED_IPS": "192.0.2.10/32 198.51.100.0/24",
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


OPS_CSP_REQUIRED = (
    "default-src 'none'",
    "script-src 'self' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'none'",
)


def check_ops_host(probe: Probe, port: int, ops: str, upstream: Upstream) -> None:
    """The ops console host: allow-list first, then only /django-admin* and /static/* reach Django; /media/
    (the console's image previews) is served from the volume; a professional's photo upload fits the body
    limit."""
    login = request(port, ops, "GET", "/django-admin/login/", headers={"X-Forwarded-For": "198.51.100.7"})
    probe.check(login.status == 200, f"ops: allowed address, login page expected 200, got {login.status}")
    probe.check("/django-admin/login/" in upstream.paths(), "ops: the login page did not reach the upstream")
    with upstream.lock:
        seen = [entry for entry in upstream.requests if entry.path == "/django-admin/login/"]
    probe.check(
        bool(seen) and seen[-1].headers.get("X-Forwarded-For") == "127.0.0.1",
        "ops: X-Forwarded-For must be the TCP peer only (a spoofed hop reached Django)",
    )
    csp = login.headers.get("content-security-policy", "")
    for directive in OPS_CSP_REQUIRED:
        probe.check(directive in csp, f"ops: Content-Security-Policy lacks {directive!r}: {csp!r}")
    probe.check(
        not re.search(r"https?:|\*", csp), f"ops: Content-Security-Policy allows another origin: {csp!r}"
    )
    probe.check(
        login.values("X-Frame-Options") == ["DENY"], f"ops: X-Frame-Options {login.values('X-Frame-Options')}"
    )
    for name, expected in (
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "same-origin"),
        ("x-robots-tag", "noindex, nofollow"),
    ):
        probe.check(login.headers.get(name) == expected, f"ops: {name} expected {expected!r}")
    probe.check("camera=()" in login.headers.get("permissions-policy", ""), "ops: Permissions-Policy missing")
    static = request(port, ops, "GET", "/static/unfold/css/styles.css")
    probe.check(static.status == 200, f"ops: /static/ expected to reach Django, got {static.status}")
    root = request(port, ops, "GET", "/")
    probe.check(
        root.status == 302 and root.headers.get("location") == "/django-admin/",
        f"ops: / expected 302 to /django-admin/, got {root.status} {root.headers.get('location')}",
    )
    before = len(upstream.paths())
    media = request(port, ops, "GET", "/media/a.txt")
    probe.check(
        media.status == 200
        and media.body == b"media"
        and media.headers.get("x-content-type-options") == "nosniff",
        f"ops: /media/ (the console's image previews) expected 200 with nosniff, got {media.status}",
    )
    probe.check("/media/a.txt" not in upstream.paths()[before:], "ops: /media/ reached the upstream")
    photo = request(
        port, ops, "POST", "/django-admin/professionals/professional/add/", body=b"x" * (5 * MIB + MIB // 2)
    )
    probe.check(
        photo.status == 200, f"ops: a 5.5 MiB photo upload expected to reach Django, got {photo.status}"
    )
    too_big = request(
        port, ops, "POST", "/django-admin/professionals/professional/add/", body=b"x" * (7 * MIB)
    )
    probe.check(too_big.status == 413, f"ops: a 7 MiB body expected 413, got {too_big.status}")
    for path in ("/api/v1/health/", "/api/v1/admin/auth/me/", "/admin"):
        before = len(upstream.paths())  # the other hosts' checks may have sent the same path legitimately
        other = request(port, ops, "GET", path)
        probe.check(
            other.status == 404 and other.body == b"",
            f"ops: {path} expected an empty 404, got {other.status}",
        )
        probe.check(path not in upstream.paths()[before:], f"ops: {path} reached the upstream")
    request(port, ops, "GET", f"/django-admin/accounts/customer/?q={SECRETS['ops_phone']}")
    before = len(upstream.paths())
    for path in ("/django-admin/login/", "/", "/static/unfold/css/styles.css", "/media/a.txt"):
        denied = request(port, ops, "GET", path, source=OUTSIDE_ADDRESS)
        probe.check(
            denied.status == 404 and denied.body == b"",
            f"ops: {path} from an address outside OPS_ALLOWED_IPS expected an empty 404, got {denied.status}",
        )
        probe.check(
            "content-security-policy" in denied.headers, f"ops: the 404 of {path} lacks the security headers"
        )
    probe.check(len(upstream.paths()) == before, "ops: a request from outside the allow-list reached Django")


def run(caddy: str) -> Probe:
    probe = Probe()
    upstream = Upstream()
    server = stub_server(upstream)
    upstream_port = server.server_address[1]
    api, admin, site, www = HOSTS["API_DOMAIN"], HOSTS["ADMIN_DOMAIN"], HOSTS["SITE_DOMAIN"], "www.site.test"
    ops = HOSTS["OPS_DOMAIN"]
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
            for host, path in (
                (api, "/api/v1/health/"),
                (admin, "/admin"),
                (site, "/"),
                (www, "/x?y=1"),
                (ops, "/django-admin/login/"),
            ):
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
            check_ops_host(probe, port, ops, upstream)
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
