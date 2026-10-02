"""Output that tells a first-time builder what to do next.

The helper prints an `action:` line for every failure. A builder outside the
project follows that line, so it must name the real cause: a stopped
participant is not an API contract break, and an unknown id is a wrong id.
"""

import unittest

from support import CommandTests, StubService

import cli_support
import vault

TRACE = "trace=01234567...abcdef"


def failure(method: str, path: str, status: int, tag: str | None = None) -> str:
    details = f" (service_error={tag}; {TRACE})" if tag else f" ({TRACE})"
    return f"{method} {path} failed with HTTP {status}.{details}"


def problem(tag: str) -> dict:
    return {"type": f"tag:kraken.com,2025:{tag}", "title": tag}


class ServiceErrorActionTests(unittest.TestCase):
    def test_an_unavailable_participant_points_to_the_status_read(self) -> None:
        action = cli_support._action_for(
            failure("GET", "/kwal/participant/v1/funding", 503, "ParticipantUnavailable")
        )
        self.assertIn("python3 scripts/register.py status", action)
        self.assertIn("operator", action)
        self.assertNotIn("API contract", action)

    def test_an_unknown_id_asks_for_the_printed_id(self) -> None:
        action = cli_support._action_for(
            failure("GET", "/kwal/participant/v1/products/prd_x", 404, "ParticipantNotFound")
        )
        self.assertIn("id", action)
        self.assertNotIn("API contract", action)

    def test_a_rejected_session_keeps_the_credentials(self) -> None:
        action = cli_support._action_for(
            failure("GET", "/kwal/participant/v1/status", 401, "ParticipantUnauthenticated")
        )
        self.assertIn("python3 scripts/register.py check", action)
        self.assertIn("do not register again", action)

    def test_a_rejected_input_asks_for_a_correction(self) -> None:
        action = cli_support._action_for(
            failure("POST", "/kwal/participant/v1/quotes", 400, "ParticipantBadRequest")
        )
        self.assertIn("Correct", action)
        self.assertNotIn("API contract", action)

    def test_a_refused_setup_names_the_owner_address(self) -> None:
        action = cli_support._action_for(
            failure("POST", "/kwal/participant/v1/initialize", 400, "ParticipantBadRequest")
        )
        self.assertIn("owner address", action)
        self.assertIn("python3 scripts/register.py status", action)
        self.assertIn("do not register again", action)
        self.assertNotIn("product", action)

    def test_a_refused_funding_question_points_to_a_fresh_quote(self) -> None:
        action = cli_support._action_for(
            failure(
                "GET", "/kwal/participant/v1/funding?quoteId=q_1", 400,
                "ParticipantFundingRequest",
            )
        )
        self.assertIn("references/quotes.md", action)

    def test_a_gateway_failure_without_a_tag_follows_the_retry_limit(self) -> None:
        for status in (502, 503, 504):
            with self.subTest(status=status):
                action = cli_support._action_for(
                    failure("GET", "/kwal/participant/v1/status", status)
                )
                self.assertIn("references/debug.md#retries", action)

    def test_an_unanswered_checkout_reads_the_payment_before_a_retry(self) -> None:
        for message in (
            failure("POST", "/kwal/participant/v1/payments", 503, "ParticipantUnavailable"),
            failure("POST", "/kwal/participant/v1/payments", 502),
            "POST /kwal/participant/v1/payments did not complete.",
        ):
            with self.subTest(message=message):
                action = cli_support._action_for(message)
                self.assertIn("A payment may exist", action)
                self.assertIn('python3 scripts/register.py payment "<payment-id>"', action)
                self.assertIn("ParticipantNotFound", action)
                self.assertIn("references/debug.md#retries", action)
                self.assertNotIn("register.py status", action)

    def test_an_interrupted_request_follows_the_retry_limit(self) -> None:
        action = cli_support._action_for("GET /kwal/participant/v1/status did not complete.")
        self.assertIn("references/debug.md#retries", action)

    def test_registration_failures_keep_the_reconcile_rule(self) -> None:
        for status, tag in (
            (503, "ParticipantUnavailable"),
            (409, "ParticipantRegistrationNeedsOperator"),
        ):
            with self.subTest(tag=tag):
                action = cli_support._action_for(
                    failure("POST", "/kwal/participant/v1/register", status, tag)
                )
                self.assertIn("do not retry after an uncertain outcome", action)

    def test_an_unmapped_route_still_reports_a_contract_mismatch(self) -> None:
        action = cli_support._action_for(
            failure("GET", "/kwal/participant/v1/payments", 405)
        )
        self.assertIn("API contract", action)


OWNER = f"0x{'cd' * 20}"
VAULT = f"0x{'ab' * 20}"


class StatusCommandTests(CommandTests):
    def test_status_reads_setup_without_a_write(self) -> None:
        body = {
            "state": "PARTICIPANT_SETUP_STATE_PENDING",
            "step": "sandbox_approval",
            "ownerAddress": OWNER,
        }
        with StubService(payload=body) as service:
            code, out, err = self.run_command("status", service.url)
        self.assertEqual(code, 0, err)
        self.assertEqual(service.calls, [("GET", "/kwal/participant/v1/status")])
        self.assertIn(f"Owner address: {OWNER}", out)
        self.assertIn("Setup: processing at sandbox_approval", out)
        self.assertIn("python3 scripts/register.py setup", out)

    def test_status_explains_a_queued_vault(self) -> None:
        body = {
            "state": "PARTICIPANT_SETUP_STATE_PENDING",
            "step": "vault_deployment",
            "ownerAddress": OWNER,
        }
        with StubService(payload=body) as service:
            code, out, err = self.run_command("status", service.url)
        self.assertEqual(code, 0, err)
        self.assertEqual(service.calls, [("GET", "/kwal/participant/v1/status")])
        self.assertIn(vault.VAULT_QUEUED, out)
        self.assertIn("python3 scripts/register.py setup", out)

    def test_status_reports_an_operator_stop_as_the_next_step(self) -> None:
        body = {
            "state": "PARTICIPANT_SETUP_STATE_NEEDS_OPERATOR",
            "step": "card_enrollment",
            "ownerAddress": OWNER,
            "vaultAddress": VAULT,
            "chain": "eip155:763373",
            "cardStatus": "ACTIVE",
        }
        with StubService(payload=body) as service:
            code, out, err = self.run_command("status", service.url)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(service.calls), 1)
        self.assertIn(f"Vault address: {VAULT}", out)
        self.assertIn("Setup: stopped for operator help at card_enrollment", out)
        self.assertIn("do not register again", out)

    def test_status_before_setup_asks_for_the_owner_address(self) -> None:
        body = {"state": "PARTICIPANT_SETUP_STATE_NOT_STARTED"}
        with StubService(payload=body) as service:
            code, out, err = self.run_command("status", service.url)
        self.assertEqual(code, 0, err)
        self.assertIn("Setup: not started", out)
        self.assertIn("setup --owner-address", out)

    def test_status_reports_readiness(self) -> None:
        body = {
            "state": "PARTICIPANT_SETUP_STATE_READY",
            "ownerAddress": OWNER,
            "vaultAddress": VAULT,
            "chain": "eip155:763373",
            "cardStatus": "ACTIVE",
            "enrollmentId": "11111111-1111-4111-8111-111111111111",
            "enrollmentStatus": "PARTICIPANT_ENROLLMENT_STATUS_ACTIVE",
            "depositObserved": True,
        }
        with StubService(payload=body) as service:
            code, out, err = self.run_command("status", service.url)
        self.assertEqual(code, 0, err)
        self.assertIn("Setup: ready for checkout", out)

    def test_status_never_prints_the_token(self) -> None:
        body = {"state": "PARTICIPANT_SETUP_STATE_NOT_STARTED"}
        with StubService(payload=body) as service:
            _, out, err = self.run_command("status", service.url)
        from support import TOKEN

        self.assertNotIn(TOKEN, out + err)


class CheckWordingTests(CommandTests):
    def test_check_says_it_covers_local_configuration_only(self) -> None:
        with StubService(payload={}) as service:
            code, out, err = self.run_command("check", service.url, "--service-url", service.url)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Setup is complete.", out)
        self.assertIn("Local configuration is usable.", out)
        self.assertIn("python3 scripts/register.py status", out)
        self.assertEqual(service.calls, [])


DETAIL = {
    "product": {"productId": "prod_1", "title": "Tee"},
    "options": [
        {"name": "Size", "values": [{"optionId": "opt_m", "label": "M"}]},
        {"name": "Color", "values": [{"optionId": "opt_b", "label": "Blue"}]},
    ],
}


class VariantSelectionTests(CommandTests):
    def test_a_partial_selection_names_the_missing_options(self) -> None:
        with StubService(payload=DETAIL) as service:
            code, _, err = self.run_command(
                "variant", service.url, "prod_1", "--option", "Size=M"
            )
        self.assertEqual(code, 1)
        self.assertIn("Color", err)
        self.assertNotIn(("POST", "/kwal/participant/v1/products/prod_1/variant"), service.calls)


if __name__ == "__main__":
    unittest.main()
