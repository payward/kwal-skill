import base64
import dataclasses
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from support import SCRIPTS, StubService

import cli_support  # noqa: E402
import pws_client  # noqa: E402
import register  # noqa: E402
import session  # noqa: E402
import transport  # noqa: E402
from pws_client import (  # noqa: E402
    ConfigurationError,
    CredentialStore,
    Credentials,
    CredentialsError,
    ServiceError,
)

# The helper reads the proxy environment when it is imported, so a proxy test
# needs a separate interpreter.
_LOOPBACK_REQUEST_PROGRAM = f"""
import sys

sys.path.insert(0, {str(SCRIPTS)!r})

import pws_client

pws_client.request_json(sys.argv[1], "/protected", token="token")
"""

# The program runs in a separate interpreter, so it loads the helper itself.
_PROTECTED_REQUEST_PROGRAM = f"""
import pathlib
import sys

sys.path.insert(0, {str(SCRIPTS)!r})

import pws_client

credentials = pws_client.CredentialStore(pathlib.Path(sys.argv[1])).load()
pws_client.request_json(
    credentials.service_url, "/protected", token=credentials.token
)
"""

NOW = 1_700_000_000
SESSION_SECONDS = 604_800
LATER = NOW + SESSION_SECONDS


def wall_clock_expiry() -> int:
    """The CLI reads the real clock, so its fixtures must too."""
    return int(time.time()) + SESSION_SECONDS


def make_token(claims: dict | None = None) -> str:
    def segment(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = segment({"alg": "HS256", "typ": "JWT"})
    body = segment(claims if claims is not None else {"sub": "uuid", "exp": LATER})
    return f"{header}.{body}.c2lnbmF0dXJl"


def seed_credentials_file(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, bytearray)):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)


def resolved(path: Path) -> Path:
    """A temporary directory can be reached through a symlinked parent."""
    return path.parent.resolve() / path.name


def unreachable_url() -> str:
    with StubService() as service:
        return service.url


class ServiceUrlTests(unittest.TestCase):
    def test_https_url_is_accepted_and_trailing_slash_removed(self) -> None:
        self.assertEqual(
            pws_client.normalize_service_url("https://pws.example/"),
            "https://pws.example",
        )

    def test_plain_http_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            pws_client.normalize_service_url("http://pws.example")

    def test_loopback_http_is_allowed_for_fixtures(self) -> None:
        self.assertEqual(
            pws_client.normalize_service_url("http://127.0.0.1:8080"),
            "http://127.0.0.1:8080",
        )

    def test_authority_and_route_components_are_constrained(self) -> None:
        cases = {
            "no host": "https://",
            "userinfo": "https://trusted.example@other.example",
            "query": "https://pws.example?next=/elsewhere",
            "fragment": "https://pws.example#part",
            "path": "https://pws.example/api",
            "malformed authority": "https://pws.example:bad",
        }
        for name, candidate in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ConfigurationError):
                    pws_client.normalize_service_url(candidate)

    def test_the_documented_placeholder_host_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            pws_client.normalize_service_url("https://<gateway-host>")

    def test_empty_delimiters_never_reach_the_route(self) -> None:
        for candidate in ("https://pws.example?", "https://pws.example#"):
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    pws_client.normalize_service_url(candidate), "https://pws.example"
                )

    def test_an_empty_explicit_url_is_reported(self) -> None:
        with self.assertRaises(ConfigurationError):
            pws_client.resolve_service_url("")

    def test_an_unset_or_empty_variable_uses_the_uat_gateway(self) -> None:
        for value in (None, ""):
            with self.subTest(value=value):
                with unittest.mock.patch.dict(os.environ, clear=False) as environ:
                    environ.pop(pws_client.SERVICE_URL_ENV, None)
                    if value is not None:
                        environ[pws_client.SERVICE_URL_ENV] = value
                    self.assertEqual(
                        pws_client.resolve_service_url(),
                        "https://api.sandbox.services.payward.com",
                    )

    def test_the_variable_overrides_the_default(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {pws_client.SERVICE_URL_ENV: "https://pws.example/"}
        ):
            self.assertEqual(pws_client.resolve_service_url(), "https://pws.example")


class CredentialsPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_explicit_argument_wins_over_the_environment(self) -> None:
        explicit = Path(self.directory.name) / "explicit.json"
        environment = Path(self.directory.name) / "environment.json"
        with unittest.mock.patch.dict(
            os.environ, {pws_client.CREDENTIALS_FILE_ENV: str(environment)}
        ):
            self.assertEqual(
                pws_client.resolve_credentials_path(str(explicit)), resolved(explicit)
            )

    def test_the_environment_path_is_used_when_no_argument_is_given(self) -> None:
        environment = Path(self.directory.name) / "environment.json"
        with unittest.mock.patch.dict(
            os.environ, {pws_client.CREDENTIALS_FILE_ENV: str(environment)}
        ):
            self.assertEqual(
                pws_client.resolve_credentials_path(), resolved(environment)
            )

    def test_default_path_lives_under_the_config_home(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": self.directory.name}
        ) as environ:
            environ.pop(pws_client.CREDENTIALS_FILE_ENV, None)
            self.assertEqual(
                pws_client.resolve_credentials_path(),
                resolved(
                    Path(self.directory.name)
                    / "pws"
                    / "agent-payment"
                    / "credentials.json"
                ),
            )

    def test_a_root_home_directory_is_reported(self) -> None:
        # An empty HOME resolves to the root, and the default path under it
        # would fail only after the service issued a session.
        os.environ.pop(pws_client.CREDENTIALS_FILE_ENV, None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        with unittest.mock.patch.object(Path, "home", return_value=Path("/")):
            with self.assertRaises(ConfigurationError):
                pws_client.resolve_credentials_path(None)

    def test_a_relative_home_directory_is_reported(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}) as environ:
            environ.pop(pws_client.CREDENTIALS_FILE_ENV, None)
            environ.pop("XDG_CONFIG_HOME", None)
            with unittest.mock.patch.object(Path, "home", return_value=Path("home")):
                with self.assertRaises(ConfigurationError):
                    pws_client.resolve_credentials_path()

    def test_a_looping_path_is_refused(self) -> None:
        first = Path(self.directory.name) / "first"
        second = Path(self.directory.name) / "second"
        first.symlink_to(second, target_is_directory=True)
        second.symlink_to(first, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            pws_client.resolve_credentials_path(str(first / "credentials.json"))

    def test_a_relative_config_home_is_refused(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "."}) as environ:
            environ.pop(pws_client.CREDENTIALS_FILE_ENV, None)
            with self.assertRaises(ConfigurationError):
                pws_client.resolve_credentials_path()

    def test_a_symlinked_parent_into_the_checkout_is_refused(self) -> None:
        checkout = Path(self.directory.name) / "checkout"
        (checkout / ".git").mkdir(parents=True)
        link = Path(self.directory.name) / "link"
        link.symlink_to(checkout, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            pws_client.resolve_credentials_path(str(link / "credentials.json"))

    def test_a_path_inside_a_worktree_is_refused(self) -> None:
        checkout = Path(self.directory.name) / "worktree"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: ../repository/.git/worktrees/test\n")
        with self.assertRaises(ConfigurationError):
            pws_client.resolve_credentials_path(str(checkout / "credentials.json"))

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            pws_client.resolve_credentials_path("credentials.json")

    def test_a_path_inside_another_checkout_is_refused(self) -> None:
        other = Path(self.directory.name) / "other"
        (other / ".git").mkdir(parents=True)
        with self.assertRaises(ConfigurationError):
            pws_client.resolve_credentials_path(str(other / "credentials.json"))

    def test_a_repository_at_the_home_directory_refuses_the_default(self) -> None:
        home = Path(self.directory.name) / "home"
        (home / ".git").mkdir(parents=True)
        with unittest.mock.patch.object(Path, "home", return_value=home):
            with unittest.mock.patch.dict(os.environ, {}, clear=False) as environ:
                environ.pop(pws_client.CREDENTIALS_FILE_ENV, None)
                environ.pop("XDG_CONFIG_HOME", None)
                with self.assertRaises(ConfigurationError):
                    pws_client.resolve_credentials_path()

    def test_an_empty_path_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            pws_client.resolve_credentials_path("")
        with unittest.mock.patch.dict(
            os.environ, {pws_client.CREDENTIALS_FILE_ENV: "  "}
        ):
            with self.assertRaises(ConfigurationError):
                pws_client.resolve_credentials_path()

    def test_an_unknown_home_directory_is_reported(self) -> None:
        with unittest.mock.patch.object(Path, "home", side_effect=RuntimeError):
            with unittest.mock.patch.dict(os.environ, {}, clear=False) as environ:
                environ.pop(pws_client.CREDENTIALS_FILE_ENV, None)
                environ.pop("XDG_CONFIG_HOME", None)
                with self.assertRaises(ConfigurationError):
                    pws_client.resolve_credentials_path()


class RegistrationParseTests(unittest.TestCase):
    def test_valid_response_becomes_credentials(self) -> None:
        token = make_token()
        credentials = pws_client.parse_registration(
            "https://pws.example",
            {"token": token, "expiresAtUnixSeconds": LATER, "unknown": "ignored"},
            NOW,
        )
        self.assertEqual(credentials.expires_at, LATER)
        self.assertEqual(credentials.token, token)

    def test_a_string_expiry_is_accepted(self) -> None:
        token = make_token()
        credentials = pws_client.parse_registration(
            "https://pws.example",
            {"token": token, "expiresAtUnixSeconds": str(LATER)},
            NOW,
        )
        self.assertEqual(credentials.expires_at, LATER)

    def test_missing_token_is_rejected(self) -> None:
        with self.assertRaises(ServiceError):
            pws_client.parse_registration(
                "https://pws.example", {"expiresAtUnixSeconds": LATER}, NOW
            )

    def test_non_integer_expiry_is_rejected(self) -> None:
        cases = {
            "not a number": "soon",
            "digits of another script": "\u0661\u0667\u0660\u0660",
        }
        for name, expiry in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ServiceError):
                    pws_client.parse_registration(
                        "https://pws.example",
                        {"token": make_token(), "expiresAtUnixSeconds": expiry},
                        NOW,
                    )

    def test_a_header_unsafe_token_is_rejected(self) -> None:
        for name, token in (("newline", "abc\r\nX: y"), ("space", "abc def")):
            with self.subTest(name=name):
                with self.assertRaises(ServiceError):
                    pws_client.parse_registration(
                        "https://pws.example", {"token": token, "expiresAtUnixSeconds": LATER}, NOW
                    )

    def test_already_expired_session_is_rejected(self) -> None:
        with self.assertRaises(ServiceError):
            pws_client.parse_registration(
                "https://pws.example",
                {"token": make_token({"exp": NOW - 1}), "expiresAtUnixSeconds": NOW - 1},
                NOW,
            )

    def test_token_is_stored_verbatim_without_interpretation(self) -> None:
        credentials = pws_client.parse_registration(
            "https://pws.example", {"token": "opaque", "expiresAtUnixSeconds": LATER}, NOW
        )
        self.assertEqual(credentials.token, "opaque")


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "credentials.json"
        self.store = CredentialStore(self.path)
        self.credentials = Credentials(
            service_url="https://pws.example", token=make_token(), expires_at=LATER
        )

    def test_a_symlinked_parent_component_stops_the_save(self) -> None:
        real = Path(self.directory.name) / "real"
        real.mkdir()
        link = Path(self.directory.name) / "link"
        link.symlink_to(real, target_is_directory=True)
        store = CredentialStore(link / "credentials.json")
        # The check resolves the path, so the symlink is put back afterwards to
        # stand for a parent that an attacker swaps in after that check.
        with unittest.mock.patch.object(
            session, "refuse_a_checkout_path", side_effect=lambda path: path
        ):
            with self.assertRaises(CredentialsError):
                store.save(self.credentials)
        self.assertEqual(list(real.iterdir()), [])

    def test_a_symlinked_credentials_file_is_refused(self) -> None:
        # A link at the path can be swapped after the checks, so the load must
        # read only the file it checked.
        real = Path(self.directory.name) / "real.json"
        CredentialStore(real).save(self.credentials)
        link = Path(self.directory.name) / "link.json"
        link.symlink_to(real)
        with self.assertRaises(CredentialsError):
            CredentialStore(link).load()

    def test_the_token_stays_out_of_the_representation(self) -> None:
        # A debug log or an unhandled exception must not print the token.
        self.assertNotIn(self.credentials.token, repr(self.credentials))

    def test_saved_file_is_owner_only(self) -> None:
        self.store.save(self.credentials)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_a_restrictive_umask_keeps_the_session_readable(self) -> None:
        # An owner-bit umask would otherwise save a session that no later
        # process can read.
        previous = os.umask(0o700)
        self.addCleanup(os.umask, previous)
        self.store.save(self.credentials)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(self.store.load().token, self.credentials.token)

    def test_an_existing_directory_keeps_its_mode(self) -> None:
        self.path.parent.mkdir(parents=True)
        os.chmod(self.path.parent, 0o755)
        self.store.save(self.credentials)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_every_created_directory_is_owner_only(self) -> None:
        deep = Path(self.directory.name) / "pws" / "agent-payment" / "credentials.json"
        CredentialStore(deep).save(self.credentials)
        for directory in (deep.parent, deep.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_round_trip_preserves_the_session(self) -> None:
        self.store.save(self.credentials)
        self.assertEqual(self.store.load(), self.credentials)

    def test_overwrite_needs_an_explicit_request(self) -> None:
        self.store.save(self.credentials)
        with self.assertRaises(CredentialsError):
            self.store.save(self.credentials)
        self.store.save(self.credentials, overwrite=True)

    def test_no_staged_file_survives_a_save(self) -> None:
        self.store.save(self.credentials)
        self.store.save(self.credentials, overwrite=True)
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], [self.path.name])

    def test_a_failed_forced_save_keeps_the_saved_session(self) -> None:
        self.store.save(self.credentials)
        with unittest.mock.patch.object(
            CredentialStore, "_write_private", side_effect=OSError(13, "denied")
        ):
            with self.assertRaises(CredentialsError):
                self.store.save(
                    dataclasses.replace(self.credentials, token="replacement"),
                    overwrite=True,
                )
        self.assertEqual(self.store.load(), self.credentials)
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], [self.path.name])

    def test_a_forced_save_narrows_a_previously_wide_file(self) -> None:
        seed_credentials_file(self.path, {"placeholder": True}, mode=0o644)
        self.store.save(self.credentials, overwrite=True)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_missing_file_reports_how_to_register(self) -> None:
        with self.assertRaises(CredentialsError):
            self.store.load()

    def test_a_file_readable_by_other_users_is_refused(self) -> None:
        seed_credentials_file(
            self.path,
            {
                "service_url": "https://pws.example",
                "token": make_token(),
                "expires_at": LATER,
            },
            mode=0o644,
        )
        with self.assertRaises(CredentialsError):
            self.store.load()

    def test_a_file_with_missing_fields_is_rejected(self) -> None:
        seed_credentials_file(self.path, {"token": "abc"})
        with self.assertRaises(CredentialsError):
            self.store.load()

    def test_an_interrupted_write_is_rejected(self) -> None:
        seed_credentials_file(self.path, b'{"service_url": "https://pws.exa')
        with self.assertRaises(CredentialsError):
            self.store.load()

    def test_invalid_encoding_is_rejected(self) -> None:
        seed_credentials_file(self.path, b'{"token": "\xff\xfe"}')
        with self.assertRaises(CredentialsError):
            self.store.load()

    def test_saved_plain_http_service_url_is_rejected_on_load(self) -> None:
        seed_credentials_file(
            self.path,
            {"service_url": "http://pws.example", "token": "abc", "expires_at": LATER},
        )
        with self.assertRaises(CredentialsError):
            self.store.load()

    def test_expiry_is_reported_against_the_clock(self) -> None:
        self.assertFalse(self.credentials.is_expired(NOW))
        self.assertTrue(self.credentials.is_expired(LATER))


class RouteContractTests(unittest.TestCase):
    def test_every_route_matches_the_published_contract(self) -> None:
        # The service owns these paths, so a test that reads the same constant
        # the client sends would agree with any change to it.
        self.assertEqual(
            (
                pws_client.REGISTER_PATH,
                pws_client.STATUS_PATH,
                pws_client.INITIALIZE_PATH,
                pws_client.FUNDING_PATH,
            ),
            (
                "/kwal/participant/v1/register",
                "/kwal/participant/v1/status",
                "/kwal/participant/v1/initialize",
                "/kwal/participant/v1/funding",
            ),
        )


class ServiceCallTests(unittest.TestCase):
    def test_registration_reads_the_stub_response(self) -> None:
        token = make_token()
        with StubService(payload={"token": token, "expiresAtUnixSeconds": LATER}) as service:
            credentials = pws_client.register(service.url, NOW)
            self.assertEqual(service.calls, [("POST", pws_client.REGISTER_PATH)])
        self.assertEqual(credentials.token, token)
        self.assertEqual(credentials.service_url, service.url)

    def test_registration_sends_a_json_body(self) -> None:
        # A JSON-only route can reject a POST that carries no body at all.
        with StubService(payload={"token": make_token(), "expiresAtUnixSeconds": LATER}) as service:
            pws_client.register(service.url, NOW)
            self.assertEqual(service.seen_body, (b"{}", "application/json"))

    def test_registration_sends_no_authorization_header(self) -> None:
        with StubService(payload={"token": make_token(), "expiresAtUnixSeconds": LATER}) as service:
            pws_client.register(service.url, NOW)
            self.assertIsNone(service.seen_authorization)

    def test_a_saved_session_authorizes_protected_requests(self) -> None:
        token = make_token()
        with StubService(payload={"ok": True}) as service:
            pws_client.request_json(service.url, "/protected", token=token)
            self.assertEqual(service.seen_authorization, f"Bearer {token}")

    def test_a_loopback_request_never_reaches_a_proxy(self) -> None:
        # A proxy would receive the token in cleartext from the loopback
        # exception, so the request must go straight to the service.
        with StubService(payload={"ok": True}) as service:
            completed = subprocess.run(
                [sys.executable, "-c", _LOOPBACK_REQUEST_PROGRAM, service.url],
                capture_output=True,
                text=True,
                check=False,
                env=os.environ | {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(service.calls, [("GET", "/protected")])

    def test_a_redirect_never_forwards_the_token(self) -> None:
        with StubService() as elsewhere, StubService() as service:
            service.redirect_to(f"{elsewhere.url}/protected")
            with self.assertRaises(ServiceError):
                pws_client.request_json(service.url, "/protected", token=make_token())
            self.assertEqual(elsewhere.requests, [])

    def test_error_status_is_reported_without_internals(self) -> None:
        with StubService(status=503, payload={"detail": "upstream down"}) as service:
            with self.assertRaises(ServiceError) as raised:
                pws_client.register(service.url, NOW)
        self.assertIn("503", str(raised.exception))
        self.assertNotIn("upstream down", str(raised.exception))

    def test_non_object_response_is_rejected(self) -> None:
        with StubService(payload=["unexpected"]) as service:
            with self.assertRaises(ServiceError):
                pws_client.register(service.url, NOW)

    def test_malformed_bodies_are_rejected(self) -> None:
        cases = {
            "empty": b"",
            "not json": b"<html>gateway</html>",
            "invalid encoding": b'{"token": "\xff\xfe"}',
            "oversized": b'{"token": "' + b"a" * (64 * 1024) + b'"}',
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                with StubService(raw=raw) as service:
                    with self.assertRaises(ServiceError):
                        pws_client.register(service.url, NOW)

    def test_a_transport_failure_is_reported_as_a_service_error(self) -> None:
        with self.assertRaises(ServiceError):
            pws_client.register(unreachable_url(), NOW)

    def test_a_timeout_is_reported_as_a_service_error(self) -> None:
        with unittest.mock.patch.object(
            transport._OPENER, "open", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(ServiceError):
                pws_client.register("https://pws.example", NOW)

    def test_a_transport_failure_hides_the_underlying_detail(self) -> None:
        with unittest.mock.patch.object(
            transport._OPENER,
            "open",
            side_effect=urllib.error.URLError("[SSL] certificate verify failed"),
        ):
            with self.assertRaises(ServiceError) as raised:
                pws_client.register("https://pws.example", NOW)
        self.assertNotIn("SSL", str(raised.exception))

    def test_a_truncated_body_is_reported_as_a_service_error(self) -> None:
        with unittest.mock.patch.object(
            transport._OPENER,
            "open",
            side_effect=http.client.IncompleteRead(b"{"),
        ):
            with self.assertRaises(ServiceError):
                pws_client.register("https://pws.example", NOW)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.expires_at = wall_clock_expiry()
        self.token = make_token({"sub": "uuid", "exp": self.expires_at})

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = register.main(argv)
        return code, out.getvalue(), err.getvalue()

    def save_session(self, token: str, expires_at: int) -> None:
        CredentialStore(self.path).save(
            Credentials(
                service_url="https://pws.example", token=token, expires_at=expires_at
            ),
            overwrite=True,
        )

    def save_service_session(self, service_url: str) -> None:
        CredentialStore(self.path).save(
            Credentials(
                service_url=service_url,
                token=self.token,
                expires_at=self.expires_at,
            ),
            overwrite=True,
        )

    def test_a_dangling_link_stops_before_the_service_is_called(self) -> None:
        # A new session that nothing can save leaves an unheld token active.
        self.path.symlink_to(Path(self.directory.name) / "gone.json")
        payload = {"token": self.token, "expiresAtUnixSeconds": self.expires_at}
        with StubService(payload=payload) as service:
            code, out, err = self.run_main(
                ["register", "--service-url", service.url, "--credentials", str(self.path)]
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(str(self.path), err)

    def test_an_unsavable_destination_stops_before_the_service_is_called(self) -> None:
        # A new session that nothing can save leaves an unheld token active.
        blocking_file = Path(self.directory.name) / "blocking"
        blocking_file.write_text("", encoding="utf-8")
        directory_at_the_path = Path(self.directory.name) / "taken.json"
        directory_at_the_path.mkdir()
        cases = {
            "an ancestor is a file": [
                "register",
                "--credentials",
                str(blocking_file / "pws" / "credentials.json"),
            ],
            "a path is a directory": [
                "register",
                "--credentials",
                str(directory_at_the_path),
            ],
        }
        payload = {"token": self.token, "expiresAtUnixSeconds": self.expires_at}
        with StubService(payload=payload) as service:
            for name, argv in cases.items():
                with self.subTest(name=name):
                    code, out, err = self.run_main(
                        [*argv, "--service-url", service.url]
                    )
                    self.assertEqual(service.calls, [])
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertTrue("Could not save credentials" in err or "Could not read credentials" in err)

    def test_the_command_exits_non_zero_on_every_failure(self) -> None:
        with StubService(status=503, payload={}) as service:
            cases = {
                "configuration": ["show", "--credentials", "relative.json"],
                "credentials": ["show", "--credentials", str(self.path)],
                "service": [
                    "register",
                    "--service-url",
                    service.url,
                    "--credentials",
                    str(self.path),
                ],
            }
            for name, argv in cases.items():
                with self.subTest(name=name):
                    completed = subprocess.run(
                        [sys.executable, str(SCRIPTS / "register.py"), *argv],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 1, completed.stdout)
                    self.assertEqual(completed.stdout, "")
                    self.assertIn("error: ", completed.stderr)

    def test_register_saves_credentials_without_printing_the_token(self) -> None:
        payload = {"token": self.token, "expiresAtUnixSeconds": self.expires_at}
        with StubService(payload=payload) as service:
            code, out, err = self.run_main(
                ["register", "--service-url", service.url, "--credentials", str(self.path)]
            )
        self.assertEqual(code, 0)
        self.assertNotIn(self.token, out)
        self.assertNotIn(self.token, err)
        self.assertEqual(CredentialStore(self.path).load().token, self.token)

    def test_a_rejected_response_never_shows_the_token(self) -> None:
        payload = {"token": self.token, "expiresAtUnixSeconds": "soon"}
        with StubService(payload=payload) as service:
            code, out, err = self.run_main(
                ["register", "--service-url", service.url, "--credentials", str(self.path)]
            )
        self.assertEqual(code, 1)
        self.assertNotIn(self.token, out)
        self.assertNotIn(self.token, err)
        self.assertFalse(self.path.exists())

    def test_second_register_keeps_the_saved_session(self) -> None:
        payload = {"token": self.token, "expiresAtUnixSeconds": self.expires_at}
        with StubService(payload=payload) as service:
            argv = ["register", "--service-url", service.url, "--credentials", str(self.path)]
            self.run_main(argv)
            code, out, _ = self.run_main(argv)
            # A second registration must not ask the service for a new session.
            self.assertEqual(service.calls, [("POST", pws_client.REGISTER_PATH)])
        self.assertEqual(code, 0)
        self.assertIn("separate participant", out)
        self.assertEqual(CredentialStore(self.path).load().token, self.token)

    def test_register_over_an_expired_session_reports_failure(self) -> None:
        self.save_session(self.token, NOW - 1)
        code, out, err = self.run_main(
            ["register", "--credentials", str(self.path)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Session expired", err)
        self.assertNotIn("--force", err)

    def test_the_credentials_file_environment_variable_is_used_end_to_end(self) -> None:
        payload = {"token": self.token, "expiresAtUnixSeconds": self.expires_at}
        with StubService(payload=payload) as service:
            with unittest.mock.patch.dict(
                os.environ, {pws_client.CREDENTIALS_FILE_ENV: str(self.path)}
            ):
                code, _, _ = self.run_main(["register", "--service-url", service.url])
                self.assertEqual(code, 0)
                shown, out, _ = self.run_main(["show"])
        self.assertEqual(shown, 0)
        self.assertIn(str(self.path), out)
        self.assertEqual(CredentialStore(self.path).load().token, self.token)

    def test_registration_timeout_requires_reconciliation_without_retry(self) -> None:
        self.save_session(self.token, self.expires_at)
        new_path = self.path.with_name("separate.json")
        with unittest.mock.patch.object(
            transport._OPENER, "open", side_effect=TimeoutError("timed out")
        ) as request:
            code, out, err = self.run_main([
                "register", "--service-url", "https://pws.example",
                "--credentials", str(new_path),
            ])
        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn("reconcile", err)
        self.assertIn("do not retry", err)
        self.assertEqual(out, "")
        self.assertNotIn("--force", err)
        self.assertEqual(CredentialStore(self.path).load().token, self.token)

    def test_force_never_replaces_or_registers_over_existing_credentials(self) -> None:
        for contents in ("valid", "expired", "unreadable"):
            with self.subTest(contents=contents):
                if self.path.exists():
                    self.path.unlink()
                if contents == "unreadable":
                    self.path.write_text("broken")
                else:
                    self.save_session(self.token, NOW - 1 if contents == "expired" else self.expires_at)
                original = self.path.read_bytes()
                with StubService(payload={}) as service:
                    code, out, err = self.run_main([
                        "register", "--service-url", service.url,
                        "--credentials", str(self.path), "--force",
                    ])
                    self.assertEqual(service.calls, [])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("separate participant", err)
                self.assertEqual(self.path.read_bytes(), original)

    def test_separate_participant_preserves_the_original_credentials(self) -> None:
        self.save_session(self.token, self.expires_at)
        new_path = self.path.with_name("separate.json")
        replacement = make_token({"sub": "other", "exp": self.expires_at})
        with StubService(payload={"token": replacement, "expiresAtUnixSeconds": self.expires_at}) as service:
            code, _, _ = self.run_main([
                "register", "--service-url", service.url,
                "--credentials", str(new_path),
            ])
            self.assertEqual(service.calls, [("POST", pws_client.REGISTER_PATH)])
        self.assertEqual(code, 0)
        self.assertEqual(CredentialStore(self.path).load().token, self.token)
        self.assertEqual(CredentialStore(new_path).load().token, replacement)

    def test_failed_token_save_requires_reconciliation(self) -> None:
        self.save_session(self.token, self.expires_at)
        new_path = self.path.with_name("separate.json")
        payload = {"token": self.token, "expiresAtUnixSeconds": self.expires_at}
        with StubService(payload=payload) as service:
            with unittest.mock.patch.object(
                CredentialStore, "save", side_effect=CredentialsError("Could not save credentials at destination")
            ):
                code, out, err = self.run_main([
                    "register", "--service-url", service.url,
                    "--credentials", str(new_path),
                ])
            self.assertEqual(service.calls, [("POST", pws_client.REGISTER_PATH)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("reconcile", err)
        self.assertNotIn(self.token, err)
        self.assertNotIn("--force", err)
        self.assertEqual(CredentialStore(self.path).load().token, self.token)

    def test_a_saved_session_is_read_back_by_a_new_process(self) -> None:
        payload = {"token": self.token, "expiresAtUnixSeconds": self.expires_at}
        with StubService(payload=payload) as service:
            code, _, _ = self.run_main(
                [
                    "register",
                    "--service-url",
                    service.url,
                    "--credentials",
                    str(self.path),
                ]
            )
            self.assertEqual(code, 0)

            # A separate process proves the session comes from the file and
            # not from state that the registering process still holds.
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "register.py"),
                    "show",
                    "--credentials",
                    str(self.path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(str(self.path), completed.stdout)
            self.assertNotIn(self.token, completed.stdout)

            # The saved token must authorize a protected request from that
            # separate process, which proves the file holds a usable session.
            authorized = subprocess.run(
                [sys.executable, "-c", _PROTECTED_REQUEST_PROGRAM, str(self.path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(authorized.returncode, 0, authorized.stderr)
            self.assertEqual(service.calls[-1], ("GET", "/protected"))
            self.assertEqual(service.seen_authorization, f"Bearer {self.token}")
            self.assertNotIn(self.token, authorized.stdout)

    def test_show_reports_metadata_only(self) -> None:
        self.save_session(self.token, self.expires_at)
        code, out, _ = self.run_main(["show", "--credentials", str(self.path)])
        self.assertEqual(code, 0)
        self.assertNotIn(self.token, out)
        self.assertIn("https://pws.example", out)

    def test_show_without_credentials_fails_with_guidance(self) -> None:
        code, _, err = self.run_main(["show", "--credentials", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn("Register explicitly", err)

    def test_show_flags_an_expired_session(self) -> None:
        self.save_session(self.token, NOW - 1)
        code, out, err = self.run_main(["show", "--credentials", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Session expired", err)
        self.assertNotIn("--force", err)

    def test_a_short_session_reports_a_non_zero_remainder(self) -> None:
        self.assertEqual(cli_support._format_expiry(NOW + 1800, NOW), "valid for 30m")
        self.assertEqual(cli_support._format_expiry(NOW + 5400, NOW), "valid for 1h 30m")

    def test_check_reports_a_complete_setup(self) -> None:
        self.save_session(self.token, self.expires_at)
        code, out, err = self.run_main(
            [
                "check",
                "--service-url",
                "https://pws.example",
                "--credentials",
                str(self.path),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Local configuration is usable.", out)
        self.assertNotIn(self.token, out)

    def test_check_reports_every_problem_in_one_run(self) -> None:
        with unittest.mock.patch.dict(os.environ, clear=False) as environ:
            environ.pop(pws_client.SERVICE_URL_ENV, None)
            environ[pws_client.SERVICE_URL_ENV] = "http://pws.example"
            code, _, err = self.run_main(["check", "--credentials", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn(pws_client.SERVICE_URL_ENV, err)
        self.assertIn("No credentials at", err)

    def test_check_reports_the_default_gateway_without_a_variable(self) -> None:
        self.save_session(self.token, self.expires_at)
        with unittest.mock.patch.dict(os.environ, clear=False) as environ:
            environ.pop(pws_client.SERVICE_URL_ENV, None)
            code, out, err = self.run_main(["check", "--credentials", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(f"Service URL: {pws_client.DEFAULT_SERVICE_URL}", out)

    def test_check_flags_an_expired_session(self) -> None:
        self.save_session(self.token, NOW - 1)
        code, _, err = self.run_main(
            [
                "check",
                "--service-url",
                "https://pws.example",
                "--credentials",
                str(self.path),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("Session expired", err)

    def test_call_sends_the_saved_token_and_prints_the_response(self) -> None:
        with StubService(payload={"payments": []}) as service:
            self.save_service_session(service.url)
            code, out, err = self.run_main(
                ["call", "/payments", "--credentials", str(self.path)]
            )
            self.assertEqual(service.calls, [("GET", "/payments")])
            self.assertEqual(service.seen_authorization, f"Bearer {self.token}")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"payments": []})
        self.assertNotIn(self.token, out)

    def test_call_posts_a_body_file(self) -> None:
        body_file = Path(self.directory.name) / "body.json"
        body_file.write_text('{"amount": 3}', encoding="utf-8")
        with StubService(payload={"id": "p1"}) as service:
            self.save_service_session(service.url)
            code, _, _ = self.run_main(
                [
                    "call",
                    "/payments",
                    "--method",
                    "POST",
                    "--body-file",
                    str(body_file),
                    "--credentials",
                    str(self.path),
                ]
            )
            self.assertEqual(service.calls, [("POST", "/payments")])
            self.assertEqual(service.seen_body[0], b'{"amount": 3}')
        self.assertEqual(code, 0)

    def test_call_refuses_input_it_cannot_use(self) -> None:
        unreadable_body = Path(self.directory.name) / "missing.json"
        invalid_body = Path(self.directory.name) / "invalid.json"
        invalid_body.write_text("not json", encoding="utf-8")
        cases = {
            "a path without a slash": (["call", "payments"], "must start with a slash"),
            "a missing body file": (
                ["call", "/payments", "--body-file", str(unreadable_body)],
                "Could not read the request body",
            ),
            "a body file that is not JSON": (
                ["call", "/payments", "--body-file", str(invalid_body)],
                "is not JSON",
            ),
        }
        self.save_service_session("https://pws.example")
        for name, (argv, expected) in cases.items():
            with self.subTest(name=name):
                code, out, err = self.run_main(argv + ["--credentials", str(self.path)])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn(expected, err)

    def test_call_flags_an_expired_session(self) -> None:
        self.save_session(self.token, NOW - 1)
        code, out, err = self.run_main(
            ["call", "/payments", "--credentials", str(self.path)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Session expired", err)

    def test_every_failure_reports_an_action(self) -> None:
        self.save_session(self.token, NOW - 1)
        code, _, err = self.run_main(["show", "--credentials", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn("action: Preserve the credentials and contact the operator", err)

    def test_an_unmapped_failure_reports_the_general_action(self) -> None:
        self.assertEqual(
            cli_support._action_for("The service refused the connection."),
            "Report the error line to the user and stop.",
        )

    def test_every_mapped_action_matches_a_real_message(self) -> None:
        # A prefix that no message starts with reports nothing to the user.
        messages = [
            f"{pws_client.SERVICE_URL_ENV} is empty",
            f"{pws_client.SERVICE_URL_ENV} must be an https URL; got scheme 'http'",
            "XDG_CONFIG_HOME must be an absolute path",
            "Credentials path is empty",
            "No home directory",
            "Credentials must be stored outside a repository checkout",
            f"No credentials at {self.path}.",
            f"Credentials already exist at {self.path}.",
            "Credentials replacement is unsupported.",
            f"Could not read credentials at {self.path}.",
            f"Credentials at {self.path} is not a regular file.",
            f"Could not save credentials at {self.path}: reason",
            "Session expired at 1.",
            "POST /kwal/participant/v1/register failed with HTTP 500.",
            "Registration returned an already expired session.",
            "Request path payments must start with a slash.",
            "Could not read the request body at body.json.",
            "The request body at body.json is not JSON.",
            "Owner address must be 0x followed by 40 hexadecimal digits.",
            "Setup has not started for this session.",
            "Setup already uses owner address 0x1.",
            "Setup needs an operator at card enrollment.",
            "Setup response is not an object.",
            "GET /kwal/participant/v1/status failed with HTTP 500.",
            "POST /kwal/participant/v1/initialize did not complete.",
            "Required amount must be a whole number of minor units.",
            "Funding response is not an object.",
            "GET /kwal/participant/v1/funding failed with HTTP 500.",
            "Identifier must be ASCII text without spaces.",
            "Search query must be printable text.",
            "Search limit must be between 1 and 50.",
            "Quantity must be between 1 and 100.",
            "Option must be stated as name=value.",
            "Product search response is not a JSON object.",
            "Product response reports an option without values.",
            "Variant response reports a purchasable variant without a price.",
            "Shipping address needs --city.",
            "Buyer email must be printable text.",
            "Quote response reports no total.",
            "Payment response is not a JSON object.",
            f"Could not read the payment record at {self.path}.",
            f"Could not save the payment record at {self.path}: reason",
            f"Could not lock or save the payment record at {self.path}.",
            f"The payment record at {self.path} is not readable.",
            "Command input is not valid: argument --query is required.",
        ]
        for prefixes, _ in cli_support._ACTIONS:
            for prefix in prefixes:
                with self.subTest(prefix=prefix):
                    self.assertTrue(
                        any(message.startswith(prefix) for message in messages)
                    )
        # A message that no prefix maps reports no recovery action to the user.
        for message in messages:
            with self.subTest(message=message):
                self.assertNotEqual(
                    cli_support._action_for(message),
                    "Report the error line to the user and stop.",
                )

    def test_register_with_an_unusable_service_url_fails(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {pws_client.SERVICE_URL_ENV: "http://pws.example"}
        ):
            code, _, err = self.run_main(["register", "--credentials", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn(pws_client.SERVICE_URL_ENV, err)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
