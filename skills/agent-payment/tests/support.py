"""Loopback fixture service shared by the skill tests.

The tests never reach the participant API, so every route answers from a
queued response instead.
"""

import http.server
import io
import json
import socketserver
import sys
import time
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

# The skill directory name is not a Python identifier, so the scripts
# directory is placed on the path directly instead of imported as a package.
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pws_client  # noqa: E402
import register  # noqa: E402
import transport  # noqa: E402
import vault  # noqa: E402

TOKEN = "header.body.signature"


def amount(minor_units: str, *, currency: str = "USDC", decimals: int = 6) -> dict:
    return {"minorUnits": minor_units, "currency": currency, "decimals": decimals}


class NoSleep(unittest.TestCase):
    """Polling uses a deterministic clock without delaying the test suite."""

    def setUp(self) -> None:
        self.elapsed = 0.0

        def sleep(seconds):
            self.elapsed += seconds

        patch = unittest.mock.patch.object(time, "sleep", side_effect=sleep)
        self.sleep = patch.start()
        self.addCleanup(patch.stop)
        clock = SimpleNamespace(monotonic=lambda: self.elapsed, sleep=self.sleep)
        for module in (vault, transport):
            patch = unittest.mock.patch.object(module, "time", clock)
            patch.start()
            self.addCleanup(patch.stop)


class StubHandler(http.server.BaseHTTPRequestHandler):
    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.server.bodies.append(
            (self.rfile.read(length), self.headers.get("Content-Type"))
        )
        self.server.requests.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )

        if self.server.location is not None:
            self.send_response(302)
            self.send_header("Location", self.server.location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        status, payload = self.server.take()
        body = (
            self.server.raw
            if self.server.raw is not None
            else json.dumps(payload).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args) -> None:
        pass


class StubServer(http.server.HTTPServer):
    """HTTPServer without the reverse DNS lookup its bind normally does."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def take(self) -> tuple[int, object]:
        # The last queued response repeats, so a bounded poll can read a
        # steady state without a response for every attempt.
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class StubService:
    """Answers each request with the next queued response.

    A queued response is a payload, or a `(status, payload)` pair.
    """

    def __init__(
        self,
        status: int = 200,
        payload: object = None,
        *,
        responses: tuple[object, ...] = (),
        raw: bytes | None = None,
        location: str | None = None,
    ) -> None:
        self.server = StubServer(("127.0.0.1", 0), StubHandler)
        self.server.responses = [
            response if isinstance(response, tuple) else (200, response)
            for response in responses
        ] or [(status, payload)]
        self.server.raw = raw
        self.server.location = location
        self.server.requests = []
        self.server.bodies = []
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    @property
    def requests(self) -> list[tuple[str, str, str | None]]:
        return self.server.requests

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.requests]

    @property
    def authorizations(self) -> list[str | None]:
        return [authorization for _, _, authorization in self.requests]

    @property
    def bodies(self) -> list[bytes]:
        return [body for body, _ in self.server.bodies]

    @property
    def seen_authorization(self) -> str | None:
        return self.requests[-1][2] if self.requests else None

    @property
    def seen_body(self) -> tuple[bytes, str | None] | None:
        return self.server.bodies[-1] if self.server.bodies else None

    def respond_with(self, payload: object = None, *, status: int = 200) -> None:
        self.server.responses = [(status, payload)]

    def redirect_to(self, location: str) -> None:
        self.server.location = location

    def __enter__(self) -> "StubService":
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class CommandTests(NoSleep):
    """A command works from a saved session, so the harness saves one and
    captures the report the agent gives to the user."""

    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"

    def run_command(
        self,
        command: str,
        service_url: str,
        *argv: str,
        expires_at: int | None = None,
    ) -> tuple[int, str, str]:
        pws_client.CredentialStore(self.path).save(
            pws_client.Credentials(
                service_url=service_url,
                token=TOKEN,
                expires_at=(
                    int(time.time()) + 604_800 if expires_at is None else expires_at
                ),
            ),
            overwrite=True,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = register.main([command, "--credentials", str(self.path), *argv])
        return code, out.getvalue(), err.getvalue()
