import json
import unittest
import unittest.mock

from support import TOKEN, CommandTests, NoSleep, StubService

import pws_client  # noqa: E402
import vault  # noqa: E402
from pws_client import ConfigurationError, ServiceError  # noqa: E402

VAULT = f"0x{'ab' * 20}"
OWNER = f"0x{'cd' * 20}"

READY = {
    "state": "PARTICIPANT_SETUP_STATE_READY",
    "ownerAddress": OWNER,
    "vaultAddress": VAULT,
    "chain": "ink-sepolia",
    "cardStatus": "ACTIVE",
    "enrollmentId": "11111111-1111-4111-8111-111111111111",
    "enrollmentStatus": "PARTICIPANT_ENROLLMENT_STATUS_ACTIVE",
    "depositObserved": True,
}
PENDING = {"ownerAddress": OWNER, "state": "PARTICIPANT_SETUP_STATE_PENDING", "step": "card enrollment"}
NOT_STARTED = {"state": "PARTICIPANT_SETUP_STATE_NOT_STARTED"}
NEEDS_OPERATOR = {
    "ownerAddress": OWNER,
    "state": "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR",
    "step": "card enrollment",
}


class OwnerAddressTests(unittest.TestCase):
    def test_an_address_is_accepted_as_written(self) -> None:
        self.assertEqual(pws_client.normalize_owner_address(f"  {OWNER}  "), OWNER)

    def test_a_malformed_address_is_refused(self) -> None:
        cases = {
            "no prefix": OWNER[2:],
            "too short": OWNER[:-2],
            "too long": f"{OWNER}ab",
            "not hexadecimal": f"0x{'zz' * 20}",
            "empty": "",
            "zero address": f"0x{'0' * 40}",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ConfigurationError):
                    pws_client.normalize_owner_address(value)


class SetupParseTests(unittest.TestCase):
    def test_a_ready_state_carries_the_vault_and_chain(self) -> None:
        setup = pws_client.parse_setup(READY)
        self.assertEqual(setup.state, "ready")
        self.assertEqual(setup.vault_address, VAULT)
        self.assertEqual(setup.chain, "ink-sepolia")

    def test_a_pending_state_carries_the_current_step(self) -> None:
        setup = pws_client.parse_setup(PENDING)
        self.assertEqual(setup.state, "pending")
        self.assertEqual(setup.step, "card enrollment")
        self.assertIsNone(setup.vault_address)

    def test_an_operator_state_carries_the_step(self) -> None:
        setup = pws_client.parse_setup(NEEDS_OPERATOR)
        self.assertEqual(setup.state, "needs_operator")
        self.assertEqual(setup.step, "card enrollment")

    def test_a_not_started_state_carries_no_progress(self) -> None:
        setup = pws_client.parse_setup(NOT_STARTED)
        self.assertEqual(setup.state, "not_started")
        self.assertIsNone(setup.step)
        self.assertIsNone(setup.vault_address)

    def test_a_malformed_response_is_rejected(self) -> None:
        cases = {
            "not an object": ["ready"],
            "no state": {"chain": "ink-sepolia"},
            "unknown state": {"state": "PARTICIPANT_SETUP_STATE_FINISHED"},
            "unprefixed state": {"state": "ready"},
            "state is not text": {"state": 3},
            "chain is not text": PENDING | {"chain": 763373},
            "vault is not an address": READY | {"vaultAddress": "not-an-address"},
            "not started with a vault": NOT_STARTED | {"vaultAddress": VAULT},
            "not started with a step": NOT_STARTED | {"step": "vault"},
            "not started with an enrollment": NOT_STARTED | {
                "enrollmentId": READY["enrollmentId"],
                "enrollmentStatus": READY["enrollmentStatus"],
            },
            "step is not displayable": PENDING | {"step": "card\x1b[2Jenrollment"},
            "step is too long": PENDING | {"step": "a" * 201},
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ServiceError):
                    pws_client.parse_setup(body)

    def test_optional_owner_and_card_evidence_is_preserved(self) -> None:
        setup = pws_client.parse_setup(READY | {"ownerAddress": OWNER, "cardStatus": "ACTIVE"})
        self.assertEqual(setup.owner_address, OWNER)
        self.assertEqual(setup.card_status, "ACTIVE")
        self.assertIsNone(pws_client.parse_setup(READY | {"ownerAddress": None}).owner_address)

    def test_invalid_optional_evidence_is_rejected(self) -> None:
        for body in (
            READY | {"ownerAddress": "invalid"},
            READY | {"cardStatus": "ENROLLED"},
            NOT_STARTED | {"ownerAddress": OWNER},
            NOT_STARTED | {"cardStatus": "ACTIVE"},
            READY | {"chain": None},
        ):
            with self.subTest(body=body), self.assertRaises(ServiceError):
                pws_client.parse_setup(body)

    def test_ready_without_a_vault_is_rejected(self) -> None:
        body = {key: value for key, value in READY.items() if key != "vaultAddress"}
        with self.assertRaises(ServiceError):
            pws_client.parse_setup(body)


class SetupServiceTests(NoSleep):
    def test_initialize_cannot_report_that_setup_never_started(self) -> None:
        with StubService(responses=(NOT_STARTED,)) as service:
            with self.assertRaisesRegex(ServiceError, "lost the initialized setup"):
                pws_client.initialize_setup(service.url, TOKEN, OWNER)
            self.assertEqual(service.calls, [("POST", pws_client.INITIALIZE_PATH)])

    def test_missing_or_malformed_http_setup_is_rejected(self) -> None:
        for body in (None, [], "invalid", {}, {"setup": READY}):
            for initialize in (False, True):
                with self.subTest(body=body, initialize=initialize):
                    with StubService(responses=(body,)) as service:
                        with self.assertRaises(ServiceError):
                            if initialize:
                                pws_client.initialize_setup(service.url, TOKEN, OWNER)
                            else:
                                pws_client.read_setup(service.url, TOKEN)

    def test_initialization_sends_the_owner_address_under_the_saved_token(self) -> None:
        with StubService(responses=(PENDING,)) as service:
            setup = pws_client.initialize_setup(service.url, TOKEN, OWNER)
            self.assertEqual(service.calls, [("POST", pws_client.INITIALIZE_PATH)])
            self.assertEqual(json.loads(service.bodies[0]), {"ownerAddress": OWNER})
            self.assertEqual(service.authorizations, [f"Bearer {TOKEN}"])
        self.assertEqual(setup.state, "pending")

    def test_a_status_read_sends_no_body(self) -> None:
        with StubService(responses=(NOT_STARTED,)) as service:
            pws_client.read_setup(service.url, TOKEN)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])
            self.assertEqual(service.bodies, [b""])

    def test_polling_returns_the_settled_state(self) -> None:
        with StubService(responses=(PENDING, PENDING, READY,)) as service:
            setup = pws_client.poll_setup(
                service.url, TOKEN, pws_client.parse_setup(PENDING)
            )
            self.assertEqual(service.calls.count(("GET", pws_client.STATUS_PATH)), 3)
        self.assertEqual(setup.state, "ready")

    def test_polling_is_bounded_and_starts_no_second_setup(self) -> None:
        with StubService(responses=(PENDING,)) as service:
            setup = pws_client.poll_setup(
                service.url,
                TOKEN,
                pws_client.parse_setup(PENDING),
                attempts=4,
            )
            self.assertEqual(len(service.calls), 4)
            self.assertNotIn(("POST", pws_client.INITIALIZE_PATH), service.calls)
        self.assertEqual(setup.state, "pending")
        self.assertEqual(
            self.sleep.call_args_list,
            [unittest.mock.call(pws_client.POLL_INTERVAL_SECONDS)] * 4,
        )

    def test_a_settled_state_is_never_polled(self) -> None:
        with StubService(responses=(READY,)) as service:
            pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(READY))
            self.assertEqual(service.calls, [])

    def test_a_reflected_token_is_never_returned_for_display(self) -> None:
        reflections = {
            "chain": PENDING | {"chain": f"rejected {TOKEN}"},
            "step": PENDING | {"step": f"rejected {TOKEN}"},
            "enrollment_id": PENDING
            | {
                "enrollmentId": f"rejected {TOKEN}",
                "enrollmentStatus": "PARTICIPANT_ENROLLMENT_STATUS_ACTIVE",
            },
        }
        for field, body in reflections.items():
            with self.subTest(field=field):
                with StubService(responses=(body,)) as service:
                    setup = pws_client.read_setup(service.url, TOKEN)
                text = getattr(setup, field)
                self.assertNotIn(TOKEN, text)
                self.assertIn("[redacted]", text)

    def test_a_token_returned_inside_an_address_is_refused(self) -> None:
        with StubService(responses=(READY,)) as service:
            with self.assertRaises(ServiceError):
                pws_client.read_setup(service.url, "ab")

    def test_a_service_error_hides_the_service_detail(self) -> None:
        with StubService(responses=((503, {"detail": "vault factory down"}),)) as service:
            with self.assertRaises(ServiceError) as raised:
                pws_client.read_setup(service.url, TOKEN)
        self.assertIn("503", str(raised.exception))
        self.assertNotIn("vault factory down", str(raised.exception))


class SetupCommandTests(CommandTests):
    def run_setup(
        self, service_url: str, *argv: str, expires_at: int | None = None
    ) -> tuple[int, str, str]:
        return self.run_command("setup", service_url, *argv, expires_at=expires_at)

    def test_an_issued_card_hands_off_to_funding_before_checkout_is_ready(self) -> None:
        pending = READY | {"state": "PARTICIPANT_SETUP_STATE_PENDING", "step": "deposit_observation", "cardStatus": "ACTIVE", "enrollmentId": None, "enrollmentStatus": None}
        with StubService(responses=(pending,)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertTrue(all(method == "GET" for method, _ in service.calls))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Sandbox card: active", out)
        self.assertIn("run the funding command", out)
        # Past the enrollment step there is no enrollment left to ask for.
        self.assertNotIn("still needs card enrollment", out)
        self.assertNotIn("ready for checkout", out)

    def test_an_unenrolled_card_asks_for_enrollment_at_that_step(self) -> None:
        pending = READY | {"state": "PARTICIPANT_SETUP_STATE_PENDING", "step": "card_enrollment", "cardStatus": "ACTIVE", "enrollmentId": None, "enrollmentStatus": None}
        with StubService(responses=(pending,)) as service:
            code, out, err = self.run_setup(service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("still needs card enrollment", out)

    def test_a_simulated_payment_sandbox_is_ready_without_enrollment(self) -> None:
        ready = READY | {"enrollmentId": None, "enrollmentStatus": None}
        with StubService(responses=(ready,)) as service:
            code, out, err = self.run_setup(service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ready for checkout", out)
        self.assertNotIn("Enrollment:", out)

    def test_an_owner_address_starts_setup_and_reports_progress(self) -> None:
        with StubService(responses=(NOT_STARTED, PENDING, READY,)) as service:
            code, out, err = self.run_setup(service.url, "--owner-address", OWNER)
            self.assertEqual(
                service.calls,
                [
                    ("GET", pws_client.STATUS_PATH),
                    ("POST", pws_client.INITIALIZE_PATH),
                    ("GET", pws_client.STATUS_PATH),
                ],
            )
            self.assertEqual(json.loads(service.bodies[1]), {"ownerAddress": OWNER})
            self.assertEqual(service.authorizations, [f"Bearer {TOKEN}"] * 3)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(VAULT, out)
        self.assertIn("ink-sepolia", out)
        self.assertIn("ready for checkout", out)
        self.assertIn("run the funding command", out)
        self.assertNotIn(TOKEN, out)

    def test_a_started_setup_is_observed_without_an_owner_address(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(VAULT, out)

    def test_an_owner_address_starts_nothing_for_an_existing_setup(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, out, _ = self.run_setup(service.url, "--owner-address", OWNER)
            self.assertNotIn(("POST", pws_client.INITIALIZE_PATH), service.calls)
        self.assertEqual(code, 0)
        self.assertIn(OWNER, out)

    def test_a_malformed_owner_address_is_refused_before_any_service_call(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, out, err = self.run_setup(
                service.url, "--owner-address", "<vault owner address>"
            )
            self.assertEqual(service.calls, [])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Owner address", err)

    def test_a_setup_that_needs_an_operator_while_polling_is_reported(self) -> None:
        with StubService(responses=(PENDING, NEEDS_OPERATOR)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)] * 2)
        self.assertEqual(code, 1)
        self.assertIn("processing at card enrollment", out)
        self.assertIn("card enrollment", err)
        self.assertIn("operator", err)
        self.assertIn("action: ", err)

    def test_a_reflected_token_never_reaches_the_terminal(self) -> None:
        for field in ("chain", "step"):
            with self.subTest(field=field):
                with StubService(
                    responses=(PENDING | {field: f"rejected {TOKEN}"},)
                ) as service:
                    code, out, err = self.run_setup(service.url)
                self.assertEqual(code, 0)
                self.assertNotIn(TOKEN, out + err)

    def test_an_unstarted_setup_asks_for_the_owner_address(self) -> None:
        with StubService(responses=(NOT_STARTED,)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertNotIn(("POST", pws_client.INITIALIZE_PATH), service.calls)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--owner-address", err)

    def test_an_operator_setup_reports_the_step_and_operator_help(self) -> None:
        with StubService(responses=(NEEDS_OPERATOR,)) as service:
            code, out, err = self.run_setup(service.url)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("card enrollment", err)
        self.assertIn("operator", err)
        self.assertNotIn(TOKEN, err)

    def test_an_operator_setup_without_a_step_is_still_reported(self) -> None:
        with StubService(
            responses=({"state": "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR", "ownerAddress": OWNER},)
        ) as service:
            code, out, err = self.run_setup(service.url)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("an unreported step", err)

    def test_a_pending_setup_reports_progress_before_a_later_check(self) -> None:
        with StubService(responses=(PENDING,)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertEqual(
                service.calls,
                [("GET", pws_client.STATUS_PATH)]
                * (vault.SETUP_WAIT_SECONDS // pws_client.POLL_INTERVAL_SECONDS),
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("processing at card enrollment", out)
        self.assertEqual(self.elapsed, vault.SETUP_WAIT_SECONDS)
        self.assertEqual(out.count("processing at card enrollment"), 1)
        self.assertIn("Setup wait limit reached (300 seconds)", out)
        self.assertIn(f"python3 scripts/register.py setup --credentials {self.path}", out)
        self.assertNotIn(TOKEN, out + err)

    def test_a_queued_vault_at_the_wait_limit_gives_the_resume_command(self) -> None:
        queued = PENDING | {"step": "vault_deployment"}
        with StubService(responses=(queued,)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertEqual(
                service.calls,
                [("GET", pws_client.STATUS_PATH)]
                * (vault.SETUP_WAIT_SECONDS // pws_client.POLL_INTERVAL_SECONDS),
            )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.elapsed, vault.SETUP_WAIT_SECONDS)
        self.assertEqual(out.count(vault.VAULT_QUEUED), 1)
        self.assertNotIn("wait limit reached", out)
        self.assertNotIn("not ready", out)
        self.assertIn(
            f"Next: resume the same saved setup: python3 scripts/register.py setup --credentials {self.path}",
            out,
        )

    def test_a_queued_issuer_step_at_the_wait_limit_gives_the_resume_command(self) -> None:
        queued = PENDING | {"step": "card_creation", "ownerAddress": OWNER}
        with StubService(responses=(queued,)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertTrue(service.calls.count(("POST", pws_client.INITIALIZE_PATH)) > 1)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.elapsed, vault.SETUP_WAIT_SECONDS)
        self.assertEqual(out.count(vault.ISSUER_QUEUED), 1)
        self.assertNotIn("wait limit reached", out)
        self.assertNotIn("not ready", out)
        self.assertIn(
            f"Next: resume the same saved setup: python3 scripts/register.py setup --credentials {self.path}",
            out,
        )

    def test_a_queued_vault_is_explained_once_before_later_steps(self) -> None:
        queued = PENDING | {"step": "vault_deployment"}
        issuer = PENDING | {"step": "sandbox_approval"}
        issued = PENDING | {"step": "deposit_observation", "cardStatus": "ACTIVE"}
        with StubService(responses=(queued, queued, issuer, issued)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertEqual(
                service.calls[:4],
                [("GET", pws_client.STATUS_PATH)] * 3 + [("POST", pws_client.INITIALIZE_PATH)],
            )
            self.assertEqual(service.calls.count(("POST", pws_client.INITIALIZE_PATH)), 1)
            self.assertEqual(json.loads(service.bodies[3]), {"ownerAddress": OWNER})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.count(vault.VAULT_QUEUED), 1)

    def test_a_different_saved_owner_is_rejected_without_initialization(self) -> None:
        with StubService(responses=(READY | {"ownerAddress": VAULT},)) as service:
            code, _, err = self.run_setup(service.url, "--owner-address", OWNER)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])
        self.assertEqual(code, 1)
        self.assertIn(f"Setup already uses owner address {VAULT}", err)
        self.assertIn("Use the saved owner address", err)
        self.assertNotIn("API contract", err)

    def test_initialize_response_must_match_the_requested_owner_when_reported(self) -> None:
        with StubService(responses=(NOT_STARTED, READY | {"ownerAddress": VAULT})) as service:
            code, _, err = self.run_setup(service.url, "--owner-address", OWNER)
        self.assertEqual(code, 1)
        self.assertIn("owner address that was not requested", err)

    def test_owner_first_reported_during_polling_must_match_request(self) -> None:
        with StubService(responses=(NOT_STARTED, PENDING, READY | {"ownerAddress": VAULT})) as service:
            code, _, err = self.run_setup(service.url, "--owner-address", OWNER)
        self.assertEqual(code, 1)
        self.assertIn("owner address that was not requested", err)

    def test_owner_comparison_ignores_hexadecimal_case(self) -> None:
        with StubService(responses=(READY | {"ownerAddress": OWNER.upper().replace("0X", "0x")},)) as service:
            code, out, err = self.run_setup(service.url, "--owner-address", OWNER)
            self.assertEqual(len(service.calls), 1)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("not used", out)

    def test_owner_cannot_change_or_disappear_while_polling(self) -> None:
        for owner in (None, VAULT):
            with self.subTest(owner=owner):
                with StubService(responses=(PENDING | {"ownerAddress": OWNER}, READY | {"ownerAddress": owner})) as service:
                    code, _, err = self.run_setup(service.url)
                self.assertEqual(code, 1)
                self.assertIn("owner address that was not requested", err)

    def test_known_issuer_steps_resume_once_with_the_saved_owner(self) -> None:
        for step in ("issuer_setup", "sandbox_approval", "account_creation", "account_verification",
                     "card_creation", "card_verification"):
            with self.subTest(step=step):
                pending = PENDING | {"step": step, "ownerAddress": OWNER}
                issued = pending | {"step": "deposit_observation", "cardStatus": "ACTIVE"}
                with StubService(responses=(pending, issued)) as service:
                    code, out, err = self.run_setup(service.url)
                    self.assertEqual(service.calls.count(("POST", pws_client.INITIALIZE_PATH)), 1)
                    self.assertEqual(json.loads(service.bodies[1]), {"ownerAddress": OWNER})
                self.assertEqual((code, err), (0, ""))
                self.assertIn("Sandbox card: active (issuance, not enrollment)", out)
                self.assertNotIn("ready for checkout", out)

    def test_other_pending_steps_and_legacy_responses_are_read_only(self) -> None:
        for body in (
            PENDING | {"step": "issuer_setup", "ownerAddress": None},
            PENDING | {"step": "card_enrollment", "cardStatus": "ACTIVE", "ownerAddress": None},
            PENDING | {"step": "deposit_observation", "ownerAddress": OWNER},
            PENDING | {"step": "card_enrollment", "ownerAddress": OWNER},
            PENDING | {"step": "vault_deployment", "ownerAddress": OWNER},
        ):
            with self.subTest(body=body), StubService(responses=(body,)) as service:
                code, _, err = self.run_setup(service.url)
                self.assertNotIn(("POST", pws_client.INITIALIZE_PATH), service.calls)
                self.assertEqual((code, err), (0, ""))

    def test_a_legacy_response_marks_owner_matching_unverified(self) -> None:
        with StubService(responses=(READY | {"ownerAddress": None},)) as service:
            code, out, _ = self.run_setup(service.url)
        self.assertEqual(code, 0)
        self.assertIn("owner match is unverified", out)

    def test_an_expired_session_makes_no_service_call(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, out, err = self.run_setup(service.url, expires_at=1)
            self.assertEqual(service.calls, [])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Session expired", err)



class VaultOwnerEvidenceTests(NoSleep):
    def test_a_vault_requires_chain(self) -> None:
        with self.assertRaises(ServiceError):
            pws_client.parse_setup({key: value for key, value in READY.items() if key != "chain"})

    def test_polling_refuses_a_changed_owner(self) -> None:
        moved = READY | {"ownerAddress": f"0x{'ef' * 20}"}
        with StubService(responses=(moved,)) as service:
            with self.assertRaises(ServiceError):
                pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(PENDING))
class IssuerSetupTests(CommandTests):
    def test_polling_resumes_pending_approval_with_the_same_owner(self) -> None:
        pending = PENDING | {"step": "sandbox_approval", "vaultAddress": VAULT, "chain": "ink-sepolia"}
        with StubService(responses=(pending, READY)) as service:
            result = pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(pending))
            self.assertEqual(result.state, "ready")
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH)])
            self.assertEqual(json.loads(service.bodies[1]), {"ownerAddress": OWNER})

    def test_failed_approval_resume_stops_without_another_request(self) -> None:
        pending = PENDING | {"step": "sandbox_approval"}
        with StubService(responses=(pending, (503, {"error": "unavailable"}))) as service:
            with self.assertRaises(ServiceError):
                pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(pending), attempts=3)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH)])

    def test_pending_approval_retries_are_bounded(self) -> None:
        pending = PENDING | {"step": "sandbox_approval"}
        with StubService(responses=(pending,)) as service:
            result = pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(pending), attempts=2)
            self.assertEqual(result.state, "pending")
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH)] * 2)

    def test_polling_does_not_resume_approval_with_changed_or_missing_owner(self) -> None:
        pending = PENDING | {"step": "sandbox_approval"}
        for owner in (None, VAULT):
            with self.subTest(owner=owner), StubService(responses=(pending | {"ownerAddress": owner},)) as service:
                with self.assertRaises(ServiceError):
                    pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(pending))
                self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])

    def test_polling_stops_on_operator_approval_without_writing(self) -> None:
        pending = PENDING | {"step": "sandbox_approval"}
        stopped = pending | {"state": "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR"}
        with StubService(responses=(stopped,)) as service:
            result = pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(pending))
            self.assertEqual(result.state, "needs_operator")
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])

    def test_approval_resume_response_cannot_change_or_remove_owner(self) -> None:
        pending = PENDING | {"step": "sandbox_approval"}
        for owner in (None, VAULT):
            with self.subTest(owner=owner), StubService(responses=(pending, pending | {"ownerAddress": owner})) as service:
                with self.assertRaises(ServiceError):
                    pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(pending), attempts=2)
                self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH)])

    def test_approval_resume_stops_writes_when_response_state_or_step_changes(self) -> None:
        pending = PENDING | {"step": "sandbox_approval"}
        for result in (NEEDS_OPERATOR, PENDING | {"step": "deposit_observation"}):
            with self.subTest(result=result), StubService(responses=(pending, result)) as service:
                pws_client.poll_setup(service.url, TOKEN, pws_client.parse_setup(pending), attempts=2)
                expected = [("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH)]
                if result["state"] == PENDING["state"]:
                    expected.append(("GET", pws_client.STATUS_PATH))
                self.assertEqual(service.calls, expected)

    def test_approval_resume_preserves_reconciled_deployment_evidence(self) -> None:
        pending = PENDING | {"step": "sandbox_approval", "vaultAddress": VAULT, "chain": "ink-sepolia", "deploymentTxId": "0x" + "12" * 32}
        changed = pending | {"deploymentTxId": "0x" + "34" * 32}
        expected = pws_client.parse_setup(pending)
        with StubService(responses=(pending, changed)) as service:
            with self.assertRaises(ServiceError):
                pws_client.poll_setup(service.url, TOKEN, expected, attempts=2, expected_deployment=expected)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH)])

    def run_setup(self, service_url: str, *argv: str) -> tuple[int, str, str]:
        return self.run_command("setup", service_url, *argv)

    def test_saved_vault_resumes_issuer_setup_and_reports_card_evidence(self) -> None:
        pending = PENDING | {"step": "issuer_setup", "vaultAddress": VAULT, "chain": "ink-sepolia"}
        issued = pending | {"step": "deposit_observation", "cardStatus": "ACTIVE", "enrollmentId": None, "enrollmentStatus": None}
        with StubService(responses=(pending, issued)) as service:
            code, out, err = self.run_setup(service.url)
            self.assertEqual(sum(method == "POST" for method, _ in service.calls), 1)
            self.assertEqual(json.loads(service.bodies[1]), {"ownerAddress": OWNER})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Sandbox card: active", out)
        self.assertNotIn("ready for checkout", out)

    def test_step_name_alone_does_not_claim_an_issued_card(self) -> None:
        with StubService(responses=(PENDING | {"step": "card_enrollment"},)) as service:
            code, out, err = self.run_setup(service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Sandbox card:", out)

    def test_different_saved_owner_is_rejected_without_mutation(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, _, err = self.run_setup(service.url, "--owner-address", f"0x{'ef' * 20}")
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])
        self.assertEqual(code, 1)
        self.assertIn(f"Setup already uses owner address {OWNER}", err)
        self.assertIn("Use the saved owner address", err)
        self.assertNotIn("API contract", err)



class EnrollmentSetupTests(CommandTests):
    def test_ready_requires_all_evidence(self) -> None:
        for field in ("cardStatus", "enrollmentId", "enrollmentStatus", "depositObserved"):
            with self.subTest(field=field), self.assertRaises(ServiceError):
                pws_client.parse_setup({key: value for key, value in READY.items() if key != field})
        for status in ("REQUIRES_ACTION", "FAILED", "EXPIRED", "REVOKED"):
            with self.subTest(status=status), self.assertRaises(ServiceError):
                pws_client.parse_setup(READY | {"enrollmentStatus": f"PARTICIPANT_ENROLLMENT_STATUS_{status}"})
        for present in ({"depositObserved": False}, {"cardStatus": "UNVERIFIED"}):
            with self.subTest(present=present), self.assertRaises(ServiceError):
                pws_client.parse_setup(READY | present)

    def test_an_enrollment_status_outside_the_contract_is_rejected(self) -> None:
        for status in ("ACTIVE", "PARTICIPANT_ENROLLMENT_STATUS_SETTLED",
                       "PARTICIPANT_ENROLLMENT_STATUS_UNSPECIFIED"):
            with self.subTest(status=status), self.assertRaises(ServiceError):
                pws_client.parse_setup(READY | {"enrollmentStatus": status})

    def test_a_non_active_enrollment_outside_operator_attention_is_rejected(self) -> None:
        stopped = READY | {"state": "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR", "step": "card_enrollment"}
        for state, step in (("PARTICIPANT_SETUP_STATE_PENDING", "card_enrollment"),
                            ("PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR", "deposit_observation")):
            with self.subTest(state=state, step=step), self.assertRaises(ServiceError):
                pws_client.parse_setup(stopped | {"state": state, "step": step, "enrollmentStatus": "PARTICIPANT_ENROLLMENT_STATUS_FAILED"})

    def test_saved_card_resumes_enrollment_with_only_the_saved_owner(self) -> None:
        pending = READY | {
            "state": "PARTICIPANT_SETUP_STATE_PENDING", "step": "card_enrollment",
            "enrollmentId": None, "enrollmentStatus": None,
        }
        with StubService(responses=(pending, READY)) as service:
            code, out, err = self.run_command("setup", service.url)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH)])
            self.assertEqual(json.loads(service.bodies[1]), {"ownerAddress": OWNER})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ready for checkout", out)

    def test_enrollment_id_and_status_are_reported(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, out, err = self.run_command("setup", service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Enrollment: {READY['enrollmentId']} (active)", out)

    def test_a_stopped_enrollment_preserves_known_id_in_operator_output(self) -> None:
        stopped = READY | {"state": "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR", "step": "card_enrollment"}
        for status in ("REQUIRES_ACTION", "FAILED", "EXPIRED", "REVOKED"):
            with self.subTest(status=status):
                with StubService(
                    responses=(stopped | {"enrollmentStatus": f"PARTICIPANT_ENROLLMENT_STATUS_{status}"},)
                ) as service:
                    code, out, err = self.run_command("setup", service.url)
                    self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])
                self.assertEqual((code, out), (1, ""))
                self.assertIn(READY["enrollmentId"], err)
                self.assertIn(status, err)
                self.assertIn("card_enrollment", err)

    def test_enrollment_output_rejects_terminal_controls(self) -> None:
        with self.assertRaises(ServiceError):
            pws_client.parse_setup(READY | {"enrollmentId": "bad\x1b[2J"})

    def test_incomplete_enrollment_is_rejected(self) -> None:
        for field in ("enrollmentId", "enrollmentStatus"):
            with self.subTest(field=field), self.assertRaises(ServiceError):
                pws_client.parse_setup(PENDING | {field: READY[field]})


DEPLOYMENT_TX = "0x" + "12" * 32
SUBMITTED_VAULT = {
    "state": "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR",
    "step": "vault_deployment",
    "ownerAddress": OWNER,
    "vaultAddress": VAULT,
    "chain": "eip155:763373",
    "deploymentTxId": DEPLOYMENT_TX,
}
RECOVERED_VAULT = SUBMITTED_VAULT | {
    "state": "PARTICIPANT_SETUP_STATE_PENDING",
    "step": "issuer_setup",
}
RECOVERED_READY = READY | {
    "chain": SUBMITTED_VAULT["chain"],
    "deploymentTxId": DEPLOYMENT_TX,
}


class VaultReconciliationTests(CommandTests):
    def test_deployment_hash_is_optional_but_validated_when_present(self):
        self.assertIsNone(pws_client.parse_setup(PENDING).deployment_tx_id)
        self.assertEqual(pws_client.parse_setup(SUBMITTED_VAULT).deployment_tx_id, DEPLOYMENT_TX)
        for value in ("", "0x1234", "0x" + "gg" * 32, "0x" + "12" * 33, 42):
            with self.subTest(value=value), self.assertRaises(ServiceError):
                pws_client.parse_setup(SUBMITTED_VAULT | {"deploymentTxId": value})
        with self.assertRaises(ServiceError):
            pws_client.parse_setup(NOT_STARTED | {"deploymentTxId": DEPLOYMENT_TX})

    def test_reflected_token_in_a_hash_is_refused_without_exposure(self):
        body = SUBMITTED_VAULT | {"deploymentTxId": "0x" + "deadbeef" * 8}
        with StubService(responses=(body,)) as service:
            with self.assertRaises(ServiceError) as raised:
                pws_client.read_setup(service.url, "deadbeef")
        self.assertNotIn("deadbeef", str(raised.exception))

    def test_default_setup_stops_without_reconciling(self):
        with StubService(responses=(SUBMITTED_VAULT,)) as service:
            code, _, err = self.run_command("setup", service.url)
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])
        self.assertEqual(code, 1)
        self.assertIn("needs an operator at vault_deployment", err)

    def test_reconciliation_posts_once_with_the_saved_owner_then_reads_progress(self):
        with StubService(responses=(SUBMITTED_VAULT, RECOVERED_VAULT, RECOVERED_READY)) as service:
            code, out, err = self.run_command("setup", service.url, "--reconcile-vault")
            self.assertEqual(service.calls, [
                ("GET", pws_client.STATUS_PATH),
                ("POST", pws_client.INITIALIZE_PATH),
                ("GET", pws_client.STATUS_PATH),
            ])
            self.assertEqual(json.loads(service.bodies[1]), {"ownerAddress": OWNER})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ready for checkout", out)
        self.assertIn(f"Deployment transaction: {DEPLOYMENT_TX}", out)

    def test_an_unconfirmed_transaction_stays_stopped_without_retries(self):
        with StubService(responses=(SUBMITTED_VAULT, SUBMITTED_VAULT)) as service:
            code, _, err = self.run_command("setup", service.url, "--reconcile-vault")
            self.assertEqual(service.calls, [
                ("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH),
            ])
        self.assertEqual(code, 1)
        self.assertIn("needs an operator at vault_deployment", err)

    def test_a_lost_reconciliation_response_never_repeats_the_post(self):
        with StubService(responses=(SUBMITTED_VAULT, (503, {}))) as service:
            code, _, err = self.run_command("setup", service.url, "--reconcile-vault")
            self.assertEqual(service.calls, [
                ("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH),
            ])
        self.assertEqual(code, 1)
        self.assertIn("503", err)

    def test_missing_deployment_evidence_cannot_authorize_reconciliation(self):
        for field in ("ownerAddress", "vaultAddress", "chain", "deploymentTxId"):
            body = {key: value for key, value in SUBMITTED_VAULT.items() if key != field}
            with self.subTest(field=field), StubService(responses=(body,)) as service:
                code, _, _ = self.run_command("setup", service.url, "--reconcile-vault")
                self.assertEqual(code, 1)
                self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])

    def test_other_steps_or_states_are_not_reconciliation_requests(self):
        for body in (NOT_STARTED, RECOVERED_VAULT, RECOVERED_READY,
                     SUBMITTED_VAULT | {"step": "account_creation"}):
            with self.subTest(body=body), StubService(responses=(body,)) as service:
                code, _, _ = self.run_command("setup", service.url, "--reconcile-vault")
                self.assertEqual(code, 1)
                self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])

    def test_a_changed_requested_owner_is_refused_before_reconciliation(self):
        with StubService(responses=(SUBMITTED_VAULT,)) as service:
            code, _, err = self.run_command(
                "setup", service.url, "--reconcile-vault", "--owner-address", VAULT
            )
            self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])
        self.assertEqual(code, 1)
        self.assertIn(f"Setup already uses owner address {OWNER}", err)
        self.assertIn("Use the saved owner address", err)
        self.assertNotIn("API contract", err)

    def test_reconciliation_refuses_changed_or_missing_binding_in_response_or_poll(self):
        for field, value in (
            ("ownerAddress", VAULT), ("ownerAddress", None),
            ("vaultAddress", OWNER), ("vaultAddress", None),
            ("chain", "eip155:1"), ("chain", None),
            ("deploymentTxId", "0x" + "34" * 32), ("deploymentTxId", None),
        ):
            for during_poll in (False, True):
                changed = RECOVERED_READY | {field: value}
                responses = ((SUBMITTED_VAULT, RECOVERED_VAULT, changed)
                             if during_poll else (SUBMITTED_VAULT, changed))
                with self.subTest(field=field, during_poll=during_poll):
                    with StubService(responses=responses) as service:
                        code, _, _ = self.run_command("setup", service.url, "--reconcile-vault")
                        self.assertEqual(sum(method == "POST" for method, _ in service.calls), 1)
                        self.assertEqual(code, 1)

    def test_reconciliation_continues_pending_issuer_without_repeating_vault_recovery(self):
        with StubService(responses=(SUBMITTED_VAULT, RECOVERED_VAULT, RECOVERED_VAULT, RECOVERED_READY)) as service:
            code, out, err = self.run_command("setup", service.url, "--reconcile-vault")
            self.assertEqual(service.calls, [
                ("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH),
                ("GET", pws_client.STATUS_PATH), ("POST", pws_client.INITIALIZE_PATH),
            ])
            self.assertEqual(json.loads(service.bodies[3]), {"ownerAddress": OWNER})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("processing at issuer_setup", out)
        self.assertIn("ready for checkout", out)


if __name__ == "__main__":
    unittest.main()
