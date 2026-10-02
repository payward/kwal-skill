"""HTTP failures expose bounded diagnostics, never service payloads or secrets."""

import http.client
import io
import json
import unittest
from email.message import Message
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
import tempfile
from pathlib import Path
import urllib.error

from support import StubService
import register
import transport
from errors import ServiceError


TRACE = "0123456789abcdef0123456789abcdef"
TAG = "ParticipantRegistrationRejected"


def problem(tag=TAG):
    return {"type": f"tag:kraken.com,2025:{tag}", "title": tag, "source": "Service"}


class ErrorDiagnosticsTests(unittest.TestCase):
    def request_error(self, raw, *, trace=TRACE, token=None, stream=None, retry_after=None):
        headers = Message()
        headers["x-trace-id"] = trace
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        headers["Set-Cookie"] = "private-cookie"
        body = stream if stream is not None else io.BytesIO(raw)
        error = urllib.error.HTTPError("https://pws.example", 422, "private reason", headers, body)
        with patch.object(transport._OPENER, "open", side_effect=error) as opened:
            with self.assertRaises(ServiceError) as raised:
                transport.request_json("https://pws.example", "/register", method="POST", token=token)
        opened.assert_called_once()
        self.assertTrue(body.closed)
        return str(raised.exception)

    def test_known_tag_and_redacted_trace_survive_http_error(self):
        message = self.request_error(json.dumps(problem()).encode())
        self.assertIn(TAG, message)
        self.assertIn("trace=01234567...abcdef", message)
        self.assertNotIn(TRACE, message)
        self.assertNotIn("private", message)

    def test_untrusted_fields_and_unknown_tags_are_never_reported(self):
        cases = [
            {"type": "tag:kraken.com,2025:private-token", "title": "private-token"},
            {"type": [TAG], "title": TAG},
            {"type": "tag:other.example,2025:" + TAG, "title": TAG},
            [problem()],
            {"type": "tag:kraken.com,2025:" + TAG + "\nprivate-token"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                message = self.request_error(json.dumps(payload).encode(), trace="private-token")
                self.assertEqual(message, "POST /register failed with HTTP 422.")

    def test_other_payload_fields_are_ignored(self):
        payload = problem() | {"title": "private-token", "data": {"email": "private@example.com"}}
        message = self.request_error(json.dumps(payload).encode())
        self.assertIn(TAG, message)
        self.assertNotIn("private", message)

    def test_a_busy_card_names_only_a_valid_holding_payment(self):
        busy = problem("ParticipantCardBusy")
        message = self.request_error(
            json.dumps(busy | {"data": {"holdingPaymentId": "pay_0"}}).encode()
        )
        self.assertIn("service_error=ParticipantCardBusy; holding_payment=pay_0", message)
        for holder in ("", "..", "pay 0", "pay_0\nprivate", "p" * 129, ["pay_0"], 7):
            with self.subTest(holder=holder):
                message = self.request_error(
                    json.dumps(busy | {"data": {"holdingPaymentId": holder}}).encode()
                )
                self.assertIn("service_error=ParticipantCardBusy", message)
                self.assertNotIn("holding_payment", message)
        message = self.request_error(
            json.dumps(busy | {"data": {"holdingPaymentId": TRACE}}).encode(), token=TRACE
        )
        self.assertNotIn("holding_payment", message)

    def test_only_a_busy_card_names_a_holding_payment(self):
        message = self.request_error(
            json.dumps(problem() | {"data": {"holdingPaymentId": "pay_0"}}).encode()
        )
        self.assertNotIn("holding_payment", message)

    def test_malformed_oversized_and_deep_bodies_keep_http_status(self):
        for raw in (b"", b"<html>private</html>", b"\xff", b"[" * 2000, b" " * 65536 + b"{}"):
            with self.subTest(length=len(raw)):
                message = self.request_error(raw)
                self.assertIn("HTTP 422", message)
                self.assertNotIn(TAG, message)
                self.assertIn("trace=01234567...abcdef", message)

    def test_invalid_or_credential_bearing_trace_is_omitted(self):
        for trace in ("short", TRACE + "0", "g" * 32, TRACE + "\n", "a" * 100000):
            with self.subTest(length=len(trace)):
                self.assertNotIn("trace=", self.request_error(b"{}", trace=trace))
        self.assertNotIn("trace=", self.request_error(b"{}", token=TRACE))
        self.assertNotIn(TAG, self.request_error(json.dumps(problem()).encode(), token=TAG))

    def test_a_retry_after_hint_in_seconds_reaches_the_error_line(self):
        message = self.request_error(
            json.dumps(problem("ParticipantUnavailable")).encode(), retry_after="20"
        )
        self.assertIn(
            "(service_error=ParticipantUnavailable; retry_after=20s; trace=01234567...abcdef)",
            message,
        )

    def test_a_retry_after_hint_stays_inside_the_backoff_budget(self):
        self.assertIn("retry_after=300s", self.request_error(b"{}", retry_after="3600"))

    def test_a_retry_after_hint_that_is_not_seconds_is_omitted(self):
        for hint in ("", "0", "-5", "1.5", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "9" * 100):
            with self.subTest(hint=hint):
                self.assertNotIn("retry_after", self.request_error(b"{}", retry_after=hint))

    def test_failed_error_body_read_preserves_status_and_closes_stream(self):
        stream = io.BytesIO()
        with patch.object(stream, "read", side_effect=http.client.IncompleteRead(b"private")):
            message = self.request_error(b"", stream=stream)
        self.assertIn("HTTP 422", message)
        self.assertNotIn("private", message)

    def test_error_body_read_is_bounded(self):
        stream = io.BytesIO(b" " * 100000)
        with patch.object(stream, "read", wraps=stream.read) as read:
            self.request_error(b"", stream=stream)
        read.assert_called_once_with(65537)

    def test_registration_still_stops_after_one_failed_request(self):
        cases = (
            (422, TAG),
            (409, "ParticipantRegistrationNeedsOperator"),
            (429, "ParticipantRegistrationRateLimited"),
        )
        for status, tag in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                with StubService(status=status, payload=problem(tag)) as service:
                    path = Path(directory) / "credentials.json"
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = register.main([
                            "register", "--service-url", service.url,
                            "--credentials", str(path),
                        ])
                    self.assertEqual(code, 1)
                    self.assertEqual(len(service.calls), 1)
                    self.assertFalse(path.exists())
                    self.assertIn(tag, err.getvalue())
                    self.assertIn("do not retry after an uncertain outcome", err.getvalue())
                    self.assertEqual(out.getvalue(), "")
