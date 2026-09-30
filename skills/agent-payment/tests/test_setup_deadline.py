"""Setup continuation is bounded by one clock and retains the saved identity."""

import argparse
import dataclasses
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from support import TOKEN

import session
import register
import transport
import vault
from errors import ServiceError

SERVICE_URL = "https://example.invalid"
OWNER = "0x" + "cd" * 20
OTHER_OWNER = "0x" + "ef" * 20
VAULT = "0x" + "ab" * 20


def pending(step: str, **changes) -> vault.Setup:
    return dataclasses.replace(
        vault.Setup(
            state="pending",
            step=step,
            owner_address=OWNER,
            vault_address=VAULT,
            chain="ink-sepolia",
        ),
        **changes,
    )


READY = dataclasses.replace(
    pending("deposit_observation"), state="ready", step=None, card_status="ACTIVE"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedSetupApi:
    """Only the queued calls are allowed; each consumes deterministic time."""

    def __init__(self, clock: FakeClock, *responses) -> None:
        self.clock = clock
        self.responses = list(responses)
        self.calls = []

    def take(self, method, service_url, token, owner, deadline):
        self.calls.append((method, service_url, token, owner, deadline, self.clock.now))
        if not self.responses:
            raise AssertionError(f"Unexpected {method} after the scripted responses")
        expected_method, duration, response = self.responses.pop(0)
        if method != expected_method:
            raise AssertionError(f"Expected {expected_method}, got {method}")
        self.clock.now += duration
        if isinstance(response, BaseException):
            raise response
        return response

    def read(self, service_url, token, *, deadline=None):
        return self.take("GET", service_url, token, None, deadline)

    def initialize(self, service_url, token, owner, *, deadline=None):
        return self.take("POST", service_url, token, owner, deadline)


class SetupDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        for name, replacement in (
            ("monotonic", self.clock.monotonic),
            ("sleep", self.clock.sleep),
        ):
            patcher = patch(f"time.{name}", replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_api(self, *responses) -> ScriptedSetupApi:
        api = ScriptedSetupApi(self.clock, *responses)
        for name, replacement in (
            ("read_setup", api.read),
            ("initialize_setup", api.initialize),
        ):
            patcher = patch.object(vault, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        return api

    def run_setup(self, *, owner=None, credentials_path=None) -> tuple[int, str]:
        args = argparse.Namespace(
            owner_address=owner, reconcile_vault=False, credentials=credentials_path
        )
        credentials = session.Credentials(SERVICE_URL, TOKEN, 604800)
        out = io.StringIO()
        with patch.object(vault, "_session", return_value=credentials), redirect_stdout(out):
            code = vault._command_setup(args, now=0)
        return code, out.getvalue()

    def assert_saved_identity(self, api: ScriptedSetupApi, deadline: float) -> None:
        self.assertFalse(api.responses, "The helper stopped before the queued transition")
        for method, url, token, owner, seen_deadline, started_at in api.calls:
            self.assertEqual((url, token), (SERVICE_URL, TOKEN))
            self.assertEqual(owner, OWNER if method == "POST" else None)
            self.assertEqual(seen_deadline, deadline)
            self.assertLess(started_at, deadline)

    def test_resumes_each_confirmed_step_with_the_same_saved_owner(self) -> None:
        issuer = pending("issuer_setup")
        approval = pending("sandbox_approval")
        account = pending("account_verification")
        card = pending("card_verification")
        api = self.use_api(
            ("GET", 1, issuer),
            ("POST", 4, approval),
            ("GET", 1, approval),
            ("POST", 4, account),
            ("GET", 1, account),
            ("POST", 4, card),
            ("GET", 1, card),
            ("POST", 4, READY),
        )
        progress = []
        result = vault.poll_setup(
            SERVICE_URL, TOKEN, pending("vault_deployment"),
            deadline=400, on_progress=progress.append,
        )
        self.assertEqual(result, READY)
        self.assert_saved_identity(api, 400)
        for confirmed in (issuer, approval, account, card, READY):
            self.assertIn(confirmed, progress)
        self.assertEqual(self.clock.now, 132)

    def test_each_supported_pending_step_can_resume_from_a_status_read(self) -> None:
        for step in (
            "issuer_setup", "sandbox_approval", "account_creation",
            "account_verification", "card_verification", "card_enrollment",
        ):
            with self.subTest(step=step):
                saved = pending(step, card_status="ACTIVE" if step == "card_enrollment" else None)
                api = self.use_api(("GET", 0, saved), ("POST", 0, READY))
                self.assertEqual(
                    vault.poll_setup(SERVICE_URL, TOKEN, pending("vault_deployment"), deadline=400),
                    READY,
                )
                self.assert_saved_identity(api, 400)

    def test_uncertain_or_incomplete_states_are_read_without_resuming(self) -> None:
        cases = (
            pending("vault_deployment"),
            pending("unknown_future_step"),
            pending("issuer_setup", owner_address=None),
            pending("card_enrollment"),
            pending("card_enrollment", card_status="UNVERIFIED"),
            pending("card_enrollment", card_status="ACTIVE", enrollment_id="existing", enrollment_status="ACTIVE"),
        )
        for saved in cases:
            with self.subTest(setup=saved):
                api = self.use_api(("GET", 0, saved))
                self.assertEqual(
                    vault.poll_setup(SERVICE_URL, TOKEN, saved, attempts=1, deadline=400), saved
                )
                self.assert_saved_identity(api, 400)
                self.assertEqual([call[0] for call in api.calls], ["GET"])

    def test_explicit_operator_response_blocks_a_previously_resumable_step(self) -> None:
        stopped = dataclasses.replace(pending("sandbox_approval"), state="needs_operator")
        api = self.use_api(("GET", 1, stopped))
        progress = []
        result = vault.poll_setup(
            SERVICE_URL, TOKEN, pending("sandbox_approval"),
            deadline=400, on_progress=progress.append,
        )
        self.assertEqual(result, stopped)
        self.assertEqual([call[0] for call in api.calls], ["GET"])
        self.assert_saved_identity(api, 400)
        self.assertEqual(progress[-1], stopped)

    def test_initial_operator_state_never_sleeps_or_calls_the_api(self) -> None:
        stopped = dataclasses.replace(pending("issuer_setup"), state="needs_operator")
        api = self.use_api()
        self.assertEqual(vault.poll_setup(SERVICE_URL, TOKEN, stopped), stopped)
        self.assertEqual(api.calls, [])
        self.assertEqual(self.clock.sleeps, [])

    def test_post_operator_response_stops_without_another_read_or_resume(self) -> None:
        approval = pending("sandbox_approval")
        stopped = dataclasses.replace(approval, state="needs_operator")
        api = self.use_api(("GET", 0, approval), ("POST", 0, stopped))
        result = vault.poll_setup(SERVICE_URL, TOKEN, approval, deadline=400)
        self.assertEqual(result, stopped)
        self.assert_saved_identity(api, 400)
        self.assertEqual(len(self.clock.sleeps), 1)

    def test_terminal_request_failures_are_not_retried(self) -> None:
        for failing_method in ("GET", "POST"):
            with self.subTest(method=failing_method):
                failure = ServiceError("saved participant failed")
                responses = []
                if failing_method == "POST":
                    responses.append(("GET", 0, pending("issuer_setup")))
                responses.append((failing_method, 1, failure))
                api = self.use_api(*responses)
                with self.assertRaises(ServiceError) as raised:
                    vault.poll_setup(SERVICE_URL, TOKEN, pending("issuer_setup"), deadline=400)
                self.assertIs(raised.exception, failure)
                self.assert_saved_identity(api, 400)
                self.assertEqual(len(api.calls), 1 if failing_method == "GET" else 2)

    def test_contradictory_initial_owner_is_rejected_without_calls_or_progress(self) -> None:
        api = self.use_api()
        progress = []
        with self.assertRaises(ServiceError):
            vault.poll_setup(
                SERVICE_URL, TOKEN, pending("issuer_setup"),
                expected_owner=OTHER_OWNER, deadline=400, on_progress=progress.append,
            )
        self.assertEqual(api.calls, [])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(progress, [])

    def test_lost_legacy_setup_is_an_error_without_initializing_again(self) -> None:
        not_started = vault.Setup("not_started", None, None, None)
        api = self.use_api(("GET", 0, not_started))
        progress = []
        with self.assertRaisesRegex(ServiceError, "lost the saved setup"):
            vault.poll_setup(
                SERVICE_URL, TOKEN, pending("issuer_setup", owner_address=None),
                deadline=400, on_progress=progress.append,
            )
        self.assertEqual([call[0] for call in api.calls], ["GET"])
        self.assertEqual(progress, [])

    def test_owner_change_in_a_read_is_rejected_before_resume_or_progress(self) -> None:
        wrong_owner = pending("issuer_setup", owner_address=OTHER_OWNER)
        api = self.use_api(("GET", 0, wrong_owner))
        progress = []
        with self.assertRaises(ServiceError):
            vault.poll_setup(
                SERVICE_URL, TOKEN, pending("vault_deployment"),
                deadline=400, on_progress=progress.append,
            )
        self.assertNotIn(wrong_owner, progress)
        self.assertEqual([call[0] for call in api.calls], ["GET"])

    def test_missing_owner_in_a_resume_is_rejected_before_progress(self) -> None:
        issuer = pending("issuer_setup")
        missing_owner = pending("sandbox_approval", owner_address=None)
        api = self.use_api(("GET", 0, issuer), ("POST", 0, missing_owner))
        progress = []
        with self.assertRaises(ServiceError):
            vault.poll_setup(
                SERVICE_URL, TOKEN, pending("vault_deployment"),
                deadline=400, on_progress=progress.append,
            )
        self.assertNotIn(missing_owner, progress)
        self.assert_saved_identity(api, 400)

    def test_a_slow_get_exhausts_the_budget_before_a_resume(self) -> None:
        approval = pending("sandbox_approval")
        api = self.use_api(("GET", 297, approval))
        result = vault.poll_setup(
            SERVICE_URL, TOKEN, pending("vault_deployment"), deadline=400
        )
        self.assertEqual(result, approval)
        self.assertEqual(self.clock.now, 400)
        self.assert_saved_identity(api, 400)
        self.assertEqual(self.clock.sleeps, [3])

    def test_a_slow_post_exhausts_the_budget_without_another_sleep(self) -> None:
        approval = pending("sandbox_approval")
        account = pending("account_creation")
        api = self.use_api(("GET", 0, approval), ("POST", 297, account))
        result = vault.poll_setup(SERVICE_URL, TOKEN, approval, deadline=400)
        self.assertEqual(result, account)
        self.assertEqual(self.clock.now, 400)
        self.assert_saved_identity(api, 400)
        self.assertEqual(self.clock.sleeps, [3])

    def test_the_last_sleep_uses_only_the_remaining_budget(self) -> None:
        api = self.use_api()
        initial = pending("vault_deployment")
        result = vault.poll_setup(SERVICE_URL, TOKEN, initial, deadline=101.5)
        self.assertEqual(result, initial)
        self.assertEqual(self.clock.now, 101.5)
        self.assertEqual(self.clock.sleeps, [1.5])
        self.assertEqual(api.calls, [])

    def test_an_expired_deadline_starts_no_sleep_or_call(self) -> None:
        api = self.use_api()
        initial = pending("issuer_setup")
        self.assertEqual(
            vault.poll_setup(SERVICE_URL, TOKEN, initial, deadline=100), initial
        )
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(api.calls, [])

    def test_default_wait_budget_is_five_minutes(self) -> None:
        initial = pending("vault_deployment")
        with (
            patch.object(vault, "read_setup", return_value=initial) as read,
            patch.object(vault, "initialize_setup") as initialize,
        ):
            self.assertEqual(vault.poll_setup(SERVICE_URL, TOKEN, initial), initial)
        self.assertEqual(self.clock.now, 400)
        self.assertEqual(vault.SETUP_WAIT_SECONDS, 300)
        self.assertGreater(read.call_count, 10)
        self.assertLess(read.call_count, 100)
        self.assertTrue(all(call.kwargs["deadline"] == 400 for call in read.call_args_list))
        initialize.assert_not_called()

    def test_keyboard_interrupt_stops_without_a_resume(self) -> None:
        api = self.use_api()
        with patch("time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                vault.poll_setup(SERVICE_URL, TOKEN, pending("issuer_setup"), deadline=400)
        self.assertEqual(api.calls, [])

    def test_command_deadline_includes_its_initial_status_read(self) -> None:
        api = self.use_api(("GET", 300, pending("issuer_setup")))
        code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assert_saved_identity(api, 400)
        self.assertEqual(self.clock.now, 400)
        self.assertEqual(self.clock.sleeps, [])
        self.assertIn("python3 scripts/register.py setup", output)

    def test_command_deadline_includes_its_first_resume(self) -> None:
        api = self.use_api(
            ("GET", 3, pending("issuer_setup")),
            ("POST", 297, pending("sandbox_approval")),
        )
        code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assert_saved_identity(api, 400)
        self.assertEqual(self.clock.now, 400)
        self.assertEqual(self.clock.sleeps, [])
        self.assertIn("python3 scripts/register.py setup", output)

    def test_deposit_funding_hint_is_visible_before_the_first_wait(self) -> None:
        deposit = pending("deposit_observation", card_status="ACTIVE")
        api = self.use_api(("GET", 0, deposit), ("GET", 0, READY))
        credentials_path = "/tmp/saved participant.json"
        args = argparse.Namespace(
            owner_address=None, reconcile_vault=False, credentials=credentials_path,
        )
        credentials = session.Credentials(SERVICE_URL, TOKEN, 604800)
        out = io.StringIO()
        hint = f"python3 scripts/register.py funding --credentials '{credentials_path}'"

        def check_visible_before_sleep(seconds):
            self.assertIn(hint, out.getvalue())
            self.clock.sleep(seconds)

        with (
            patch.object(vault, "_session", return_value=credentials),
            patch("time.sleep", check_visible_before_sleep),
            redirect_stdout(out),
        ):
            self.assertEqual(vault._command_setup(args, now=0), 0)
        self.assert_saved_identity(api, 400)
        self.assertEqual(self.clock.sleeps, [3])
        self.assertIn("Setup: ready for checkout", out.getvalue())

    def test_operator_stop_next_command_preserves_the_selected_credentials_path(self) -> None:
        stopped = dataclasses.replace(pending("sandbox_approval"), state="needs_operator")
        api = self.use_api(("GET", 0, stopped))
        with self.assertRaises(ServiceError) as raised:
            self.run_setup(credentials_path="/tmp/saved participant.json")
        self.assert_saved_identity(api, 400)
        self.assertIn(
            "python3 scripts/register.py setup --credentials '/tmp/saved participant.json'",
            str(raised.exception),
        )

    def test_command_preserves_uncertain_request_errors_without_retrying(self) -> None:
        failure = ServiceError("POST did not complete")
        api = self.use_api(
            ("GET", 0, pending("issuer_setup")),
            ("POST", 1, failure),
        )
        with self.assertRaises(ServiceError) as raised:
            self.run_setup(credentials_path="/tmp/saved participant.json")
        self.assert_saved_identity(api, 400)
        self.assertIs(raised.exception, failure)
        self.assertEqual([call[0] for call in api.calls], ["GET", "POST"])
        self.assertEqual(self.clock.sleeps, [])

    def test_http_timeout_at_deadline_keeps_error_and_same_session_recovery(self) -> None:
        credentials_path = "/tmp/saved participant.json"
        credentials = session.Credentials(SERVICE_URL, TOKEN, 604800)
        opener = Mock()

        def read_initial(*args, **kwargs):
            self.clock.now += 299
            return pending("issuer_setup")

        def timeout_at_deadline(request, *, timeout):
            self.assertEqual(timeout, 1)
            self.clock.now += timeout
            raise TimeoutError("uncertain provider outcome")

        opener.open.side_effect = timeout_at_deadline
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(vault, "_session", return_value=credentials),
            patch.object(vault, "read_setup", side_effect=read_initial) as read,
            patch.object(transport, "_OPENER", opener),
            redirect_stdout(out), redirect_stderr(err),
        ):
            code = register.main(["setup", "--credentials", credentials_path])
        self.assertEqual(code, 1)
        self.assertEqual(self.clock.now, 400)
        self.assertEqual(self.clock.sleeps, [])
        read.assert_called_once()
        opener.open.assert_called_once()
        self.assertIn("POST /kwal/participant/v1/initialize did not complete.", err.getvalue())
        self.assertIn(
            "Before any retry, read the same saved setup without resuming: "
            "python3 scripts/register.py status --credentials '/tmp/saved participant.json'",
            err.getvalue(),
        )
        self.assertEqual(len(err.getvalue().splitlines()), 2)
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_deadline_next_command_preserves_the_selected_credentials_path(self) -> None:
        api = self.use_api(("GET", 300, pending("issuer_setup")))
        code, output = self.run_setup(credentials_path="/tmp/saved participant.json")
        self.assertEqual(code, 0)
        self.assert_saved_identity(api, 400)
        self.assertIn(
            "python3 scripts/register.py setup --credentials '/tmp/saved participant.json'",
            output,
        )

    def test_command_prints_each_confirmed_step_and_evidence_once(self) -> None:
        deployed = pending("vault_deployment")
        issuer = pending("issuer_setup")
        approval = pending("sandbox_approval")
        account = pending("account_creation")
        api = self.use_api(
            ("GET", 0, deployed),
            ("GET", 0, deployed),
            ("GET", 0, issuer),
            ("POST", 0, approval),
            ("GET", 0, approval),
            ("POST", 0, account),
            ("GET", 0, account),
            ("POST", 0, READY),
        )
        with patch.object(session, "register") as register_participant:
            code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assert_saved_identity(api, 400)
        register_participant.assert_not_called()
        for step in ("vault_deployment", "issuer_setup", "sandbox_approval", "account_creation"):
            self.assertEqual(output.count(f"Setup: processing at {step}"), 1, output)
        self.assertEqual(output.count(f"Owner address: {OWNER}"), 1, output)
        self.assertEqual(output.count(f"Vault address: {VAULT}"), 1, output)
        self.assertEqual(output.count("Chain: ink-sepolia"), 1, output)
        self.assertEqual(output.count("Setup: ready for checkout"), 1, output)


class SetupTransportDeadlineTests(unittest.TestCase):
    def test_http_timeout_is_capped_by_the_remaining_total_budget(self) -> None:
        for deadline, expected_timeout in ((None, 30), (200, 30), (110.5, 10.5)):
            with self.subTest(deadline=deadline):
                opener = Mock()
                opener.open.return_value = io.BytesIO(b'{"state":"pending"}')
                with patch.object(transport, "_OPENER", opener), patch("time.monotonic", return_value=100):
                    result = transport.request_json(
                        SERVICE_URL, vault.STATUS_PATH, token=TOKEN, deadline=deadline
                    )
                self.assertEqual(result, {"state": "pending"})
                self.assertEqual(opener.open.call_args.kwargs["timeout"], expected_timeout)
                self.assertEqual(opener.open.call_count, 1)

    def test_an_expired_deadline_refuses_http_before_opening_a_connection(self) -> None:
        for deadline in (99, 100):
            with self.subTest(deadline=deadline):
                opener = Mock()
                with patch.object(transport, "_OPENER", opener), patch("time.monotonic", return_value=100):
                    with self.assertRaises(ServiceError):
                        transport.request_json(
                            SERVICE_URL, vault.INITIALIZE_PATH, method="POST",
                            payload={"ownerAddress": OWNER}, token=TOKEN, deadline=deadline,
                        )
                opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
