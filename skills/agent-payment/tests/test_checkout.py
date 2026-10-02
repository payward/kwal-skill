"""Checkout submission and resume, against the fixture service.

The live UAT journey the ticket
asks for — two participants, one in Claude and one in Codex — runs against
the built routes and is not replaced by these checks.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support import TOKEN, CommandTests, StubService, amount

import pws_client  # noqa: E402
from pws_client import ConfigurationError, ServiceError  # noqa: E402

# A command test reads the real clock, so a valid quote and a live approval
# link both stay ahead of it.
EXPIRY = int(time.time()) + 604_800
APPROVAL_EXPIRY = int(time.time()) + 900

QUOTE = {
    "quoteId": "quote_1",
    "lines": [{"variantId": "var_1", "quantity": 2}],
    "shippingOptions": [
        {
            "shippingOptionId": "ship_standard",
            "label": "Standard",
            "price": amount("500000"),
        }
    ],
    "selectedShippingOptionId": "ship_standard",
    "subtotal": amount("3000000"),
    "shipping": amount("500000"),
    "tax": amount("100000"),
    "total": amount("3600000"),
    "expiresAtUnixSeconds": str(EXPIRY),
}

EXPIRED_QUOTE = QUOTE | {"expiresAtUnixSeconds": str(int(time.time()) - 10)}

UNSELECTED_QUOTE = {
    key: value
    for key, value in QUOTE.items()
    if key != "selectedShippingOptionId"
}

REQUIRES_ACTION = {
    "paymentId": "pay_1",
    "state": "PARTICIPANT_PAYMENT_STATE_REQUIRES_ACTION",
    "step": "hosted approval",
    "approvalUrl": "https://checkout.example.test/pay_1",
    "approvalExpiresAtUnixSeconds": str(APPROVAL_EXPIRY),
    "checkoutId": "chk_1",
}

PENDING = {
    "paymentId": "pay_1",
    "state": "PARTICIPANT_PAYMENT_STATE_PENDING",
    "step": "authorizing",
}

COMPLETED = {
    "paymentId": "pay_1",
    "state": "PARTICIPANT_PAYMENT_STATE_COMPLETED",
    "orderId": "order_1",
    "charged": amount("3600000"),
    "amount": amount("3600000"),
    "checkoutStatus": "PARTICIPANT_CHECKOUT_STATUS_COMPLETED",
    "quoteId": "quote_1",
}

DECLINED = {
    "paymentId": "pay_1",
    "state": "PARTICIPANT_PAYMENT_STATE_DECLINED",
    "step": "authorization",
    "reason": "The issuer declined the authorization.",
}


class PaymentContractTests(unittest.TestCase):
    def test_a_submitted_payment_reads_every_reported_reference(self):
        payment = pws_client.parse_payment(COMPLETED)

        self.assertEqual(payment.payment_id, "pay_1")
        self.assertEqual(payment.state, "completed")
        self.assertEqual(payment.order_id, "order_1")
        self.assertEqual(str(payment.charged), "3.600000 USDC")
        self.assertEqual(str(payment.amount), "3.600000 USDC")
        self.assertEqual(
            payment.checkout_status, "PARTICIPANT_CHECKOUT_STATUS_COMPLETED"
        )
        self.assertEqual(payment.quote_id, "quote_1")

    def test_an_omitted_default_is_not_a_reported_reference(self):
        payment = pws_client.parse_payment(PENDING)

        self.assertIsNone(payment.amount)
        self.assertIsNone(payment.checkout_status)
        self.assertIsNone(payment.quote_id)
        self.assertIsNone(payment.order_id)
        self.assertIsNone(payment.charged)
        self.assertIsNone(payment.approval_url)

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(ServiceError) as refusal:
            pws_client.parse_payment(PENDING | {"state": "PENDING"})

        self.assertIn("unknown state", str(refusal.exception))

    def test_a_waiting_payment_without_a_link_is_refused(self):
        body = {
            key: value
            for key, value in REQUIRES_ACTION.items()
            if key not in ("approvalUrl", "approvalExpiresAtUnixSeconds")
        }

        with self.assertRaises(ServiceError) as refusal:
            pws_client.parse_payment(body)

        self.assertIn("requires action without a link", str(refusal.exception))

    def test_a_link_without_an_optional_expiry_is_usable(self):
        body = {
            key: value
            for key, value in REQUIRES_ACTION.items()
            if key != "approvalExpiresAtUnixSeconds"
        }

        payment = pws_client.parse_payment(body)
        self.assertEqual(payment.approval_url, REQUIRES_ACTION["approvalUrl"])
        self.assertIsNone(payment.approval_expires_at)

    def test_a_link_the_human_cannot_open_safely_is_refused(self):
        with self.assertRaises(ServiceError) as refusal:
            pws_client.parse_payment(
                REQUIRES_ACTION | {"approvalUrl": "javascript:alert(1)"}
            )

        self.assertIn("not an https link", str(refusal.exception))

    def test_a_link_that_hides_its_host_is_refused(self):
        for link in (
            "https://checkout.example.test@evil.example/pay_1",
            "https://[oops/pay_1",
            "https:///pay_1",
        ):
            with self.subTest(link=link):
                with self.assertRaises(ServiceError):
                    pws_client.parse_payment(
                        REQUIRES_ACTION | {"approvalUrl": link}
                    )

    def test_completion_does_not_require_optional_order_details(self):
        for missing in ({"orderId"}, {"charged"}, {"orderId", "charged"}):
            with self.subTest(missing=missing):
                body = {
                    key: value for key, value in COMPLETED.items() if key not in missing
                }
                payment = pws_client.parse_payment(body)
                self.assertEqual(payment.state, "completed")
                if "orderId" in missing:
                    self.assertIsNone(payment.order_id)
                if "charged" in missing:
                    self.assertIsNone(payment.charged)

    def test_a_declined_payment_keeps_the_reported_reason(self):
        payment = pws_client.parse_payment(DECLINED)

        self.assertEqual(payment.state, "declined")
        self.assertEqual(payment.reason, DECLINED["reason"])


class AttemptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.credentials = Path(directory.name) / "credentials.json"
        self.store = pws_client.AttemptStore(self.credentials)

    def test_no_attempt_is_recorded_before_the_first_submission(self):
        self.assertIsNone(self.store.latest())
        self.assertIsNone(self.store.find("quote_1"))

    def test_a_recorded_attempt_is_found_by_its_quote(self):
        attempt = pws_client.Attempt(payment_id="pay_1", quote_id="quote_1")
        self.store.record(attempt)

        self.assertEqual(self.store.find("quote_1"), attempt)
        self.assertEqual(self.store.latest(), attempt)

    def test_every_recorded_attempt_stays_findable(self):
        for index in range(25):
            self.store.record(
                pws_client.Attempt(
                    payment_id=f"pay_{index}", quote_id=f"quote_{index}"
                )
            )

        self.assertEqual(self.store.find("quote_0").payment_id, "pay_0")
        self.assertEqual(self.store.latest().payment_id, "pay_24")

    def test_an_unreadable_record_names_the_file(self):
        self.store.path.write_text("{", encoding="utf-8")

        with self.assertRaises(ConfigurationError) as refusal:
            self.store.load()

        self.assertIn(str(self.store.path), str(refusal.exception))

    def test_a_record_that_is_not_a_list_of_attempts_is_refused(self):
        self.store.path.write_text('[{"payment_id": "pay_1"}]', encoding="utf-8")

        with self.assertRaises(ConfigurationError):
            self.store.load()


class CheckoutCommandTests(CommandTests):
    def setUp(self) -> None:
        super().setUp()
        # Successful responses must name the payment ID sent by the helper.
        payment_ids = patch(
            "checkout.new_payment_id", side_effect=("pay_1", "pay_2")
        )
        payment_ids.start()
        self.addCleanup(payment_ids.stop)

    def test_the_reviewed_total_is_shown_before_the_submission(self):
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

        self.assertEqual(code, 0)
        self.assertIn("Total: 3.600000 USDC", out)
        self.assertEqual(
            service.calls,
            [
                ("GET", "/kwal/participant/v1/quotes/quote_1"),
                ("POST", "/kwal/participant/v1/payments"),
            ],
        )

    def test_the_submission_carries_the_recorded_payment_id(self):
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            self.run_command("checkout", service.url, "quote_1")
            submitted = json.loads(service.bodies[-1])

        recorded = pws_client.AttemptStore(self.path).find("quote_1")
        self.assertEqual(recorded.quote_id, "quote_1")
        self.assertEqual(submitted["paymentId"], recorded.payment_id)
        self.assertEqual(submitted["quoteId"], "quote_1")

    def test_the_saved_session_authorizes_the_submission(self):
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            self.run_command("checkout", service.url, "quote_1")

        self.assertEqual(service.authorizations, [f"Bearer {TOKEN}"] * 2)

    def test_the_human_is_sent_to_the_hosted_page(self):
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            _, out, _ = self.run_command("checkout", service.url, "quote_1")

        self.assertIn(f"Approval link: {REQUIRES_ACTION['approvalUrl']}", out)
        self.assertIn("approve the payment on the hosted page", out)
        self.assertIn("never receives the card number", out)

    def test_a_repeated_checkout_resumes_the_recorded_attempt(self):
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            self.run_command("checkout", service.url, "quote_1")
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

            self.assertEqual(
                service.calls,
                [
                    ("GET", "/kwal/participant/v1/quotes/quote_1"),
                    ("POST", "/kwal/participant/v1/payments"),
                    ("POST", "/kwal/participant/v1/payments"),
                ],
            )
            submitted = [
                json.loads(body)["paymentId"] for body in service.bodies if body
            ]

        store = pws_client.AttemptStore(self.path)
        self.assertEqual(code, 0)
        self.assertEqual(submitted, [store.find("quote_1").payment_id] * 2)
        self.assertEqual(len(store.load()), 1)
        self.assertIn("Checkout: resuming the recorded attempt", out)

    def test_a_lost_submission_keeps_the_recorded_payment_id(self):
        failure = (500, {"message": "the service did not answer"})
        with StubService(responses=(QUOTE, failure, REQUIRES_ACTION)) as service:
            failed, _, err = self.run_command("checkout", service.url, "quote_1")
            recorded = pws_client.AttemptStore(self.path).find("quote_1")
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

            submitted = [
                json.loads(body)["paymentId"] for body in service.bodies if body
            ]

        self.assertEqual(failed, 1)
        self.assertIn("failed with HTTP 500", err)
        self.assertIsNotNone(recorded)
        self.assertEqual(code, 0)
        self.assertEqual(submitted, [recorded.payment_id] * 2)
        self.assertIn("Checkout: resuming the recorded attempt", out)
        self.assertEqual(len(pws_client.AttemptStore(self.path).load()), 1)

    def test_a_second_quote_is_submitted_under_its_own_payment_id(self):
        second = QUOTE | {"quoteId": "quote_2"}
        answer = REQUIRES_ACTION | {"paymentId": "pay_2"}
        with StubService(
            responses=(QUOTE, REQUIRES_ACTION, second, answer)
        ) as service:
            self.run_command("checkout", service.url, "quote_1")
            code, _, _ = self.run_command("checkout", service.url, "quote_2")

            self.assertEqual(
                service.calls,
                [
                    ("GET", "/kwal/participant/v1/quotes/quote_1"),
                    ("POST", "/kwal/participant/v1/payments"),
                    ("GET", "/kwal/participant/v1/quotes/quote_2"),
                    ("POST", "/kwal/participant/v1/payments"),
                ],
            )
            submitted = [
                json.loads(body)["paymentId"] for body in service.bodies if body
            ]

        store = pws_client.AttemptStore(self.path)
        self.assertEqual(code, 0)
        self.assertEqual(len(store.load()), 2)
        self.assertEqual(
            submitted,
            [store.find("quote_1").payment_id, store.find("quote_2").payment_id],
        )
        self.assertNotEqual(*submitted)

    def test_an_unreadable_record_never_reaches_the_service(self):
        pws_client.AttemptStore(self.path).path.write_text(
            "{", encoding="utf-8"
        )
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            code, _, err = self.run_command("checkout", service.url, "quote_1")

            self.assertEqual(service.calls, [])

        self.assertEqual(code, 1)
        self.assertIn(pws_client.ATTEMPTS_FILE_NAME, err)

    def test_a_trimmed_quote_id_reaches_one_canonical_route(self):
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            code, _, _ = self.run_command("checkout", service.url, " quote_1 ")

            self.assertEqual(
                service.calls,
                [
                    ("GET", "/kwal/participant/v1/quotes/quote_1"),
                    ("POST", "/kwal/participant/v1/payments"),
                ],
            )

        self.assertEqual(code, 0)
        self.assertIsNotNone(pws_client.AttemptStore(self.path).find("quote_1"))

    def test_a_quote_id_that_cannot_be_one_never_reaches_the_service(self):
        with StubService(responses=(QUOTE, REQUIRES_ACTION)) as service:
            code, _, err = self.run_command("checkout", service.url, "quote 1")

            self.assertEqual(service.calls, [])

        self.assertEqual(code, 1)
        self.assertIn("without spaces", err)
        self.assertIsNone(pws_client.AttemptStore(self.path).latest())

    def test_an_expired_quote_returns_to_the_quote_helper(self):
        with StubService(responses=(EXPIRED_QUOTE,)) as service:
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

            self.assertEqual(
                service.calls, [("GET", "/kwal/participant/v1/quotes/quote_1")]
            )

        self.assertEqual(code, 0)
        self.assertIn("Quote: expired", out)
        self.assertIsNone(pws_client.AttemptStore(self.path).latest())

    def test_a_quote_without_a_selected_shipping_option_is_not_submitted(self):
        with StubService(responses=(UNSELECTED_QUOTE,)) as service:
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

            self.assertEqual(len(service.calls), 1)

        self.assertEqual(code, 0)
        self.assertIn("Shipping: selection required", out)
        self.assertIsNone(pws_client.AttemptStore(self.path).latest())

    def test_a_quote_another_payment_reserved_is_never_submitted(self):
        with StubService(responses=(QUOTE | {"paymentId": "pay_9"},)) as service:
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

            self.assertEqual(
                service.calls, [("GET", "/kwal/participant/v1/quotes/quote_1")]
            )

        self.assertEqual(code, 0)
        self.assertIn("Next: read payment pay_9 with the payment command.", out)
        self.assertIsNone(pws_client.AttemptStore(self.path).latest())

    def test_an_expired_session_never_reaches_the_service(self):
        with StubService(responses=(QUOTE,)) as service:
            code, _, err = self.run_command(
                "checkout", service.url, "quote_1", expires_at=1
            )

            self.assertEqual(service.calls, [])

        self.assertEqual(code, 1)
        self.assertIn("Session expired", err)


class PaymentCommandTests(CommandTests):
    def _record(self, payment_id: str = "pay_1") -> None:
        pws_client.AttemptStore(self.path).record(
            pws_client.Attempt(payment_id=payment_id, quote_id="quote_1")
        )

    def test_the_same_payment_is_observed_after_a_restart(self):
        self._record()
        with StubService(responses=(COMPLETED,)) as service:
            code, out, _ = self.run_command("payment", service.url)

            self.assertEqual(
                service.calls, [("GET", "/kwal/participant/v1/payments/pay_1")]
            )

        self.assertEqual(code, 0)
        self.assertIn("Order id: order_1", out)

    def test_the_reported_checkout_references_reach_the_agent(self):
        with StubService(responses=(COMPLETED,)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

        self.assertEqual(code, 0)
        self.assertIn("Quoted amount: 3.600000 USDC", out)
        self.assertIn(
            "Checkout status: PARTICIPANT_CHECKOUT_STATUS_COMPLETED", out
        )
        self.assertIn("Attached quote: quote_1", out)

    def test_an_approval_without_an_expiry_still_sends_the_human(self):
        body = {
            key: value
            for key, value in REQUIRES_ACTION.items()
            if key != "approvalExpiresAtUnixSeconds"
        }
        with StubService(responses=(body,)) as service:
            code, out, err = self.run_command("payment", service.url, "pay_1")

        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Approval link: {REQUIRES_ACTION['approvalUrl']}", out)
        self.assertIn("Approval link expiry: not supplied by Reap", out)
        self.assertIn("approve the payment on the hosted page", out)

    def test_a_named_payment_is_observed_without_a_record(self):
        with StubService(responses=(COMPLETED,)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

            self.assertEqual(
                service.calls, [("GET", "/kwal/participant/v1/payments/pay_1")]
            )

        self.assertEqual(code, 0)
        self.assertIn("Payment: completed", out)

    def test_no_recorded_attempt_asks_for_the_payment_id(self):
        with StubService() as service:
            code, out, _ = self.run_command("payment", service.url)

            self.assertEqual(service.calls, [])

        self.assertEqual(code, 0)
        self.assertIn("Payment: none recorded", out)

    def test_polling_is_bounded_and_reports_the_state_it_reached(self):
        self._record()
        with StubService(responses=(PENDING,)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

            self.assertEqual(len(service.calls), pws_client.POLL_ATTEMPTS + 1)

        self.assertEqual(code, 0)
        self.assertIn("Payment: processing", out)
        self.assertIn("Do not submit a second checkout", out)

    def test_a_completed_payment_stops_the_polling(self):
        with StubService(responses=(PENDING, COMPLETED)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

            self.assertEqual(len(service.calls), 2)

        self.assertEqual(code, 0)
        self.assertIn("Payment: completed", out)

    def test_order_completion_does_not_wait_for_collection(self):
        for details in ({}, {"cardTransactionId": "txn_1", "collectionId": "col_1"}):
            with self.subTest(details=details):
                with StubService(responses=(COMPLETED | details,)) as service:
                    code, out, _ = self.run_command("payment", service.url, "pay_1")
                    self.assertEqual(len(service.calls), 1)

                self.assertEqual(code, 0)
                self.assertIn("Payment: completed", out)
                self.assertIn("Order id: order_1", out)
                self.assertIn("Charged: 3.600000 USDC", out)
                self.assertIn("Settlement: not verified by this response", out)
                self.assertNotIn("collection", out.lower())
                self.assertNotIn("Card transaction", out)
                self.assertNotIn("again later", out)

    def test_completion_without_order_details_stops_polling(self):
        body = {
            key: value
            for key, value in COMPLETED.items()
            if key not in {"orderId", "charged"}
        }
        with StubService(responses=(PENDING, body)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")
            self.assertEqual(len(service.calls), 2)

        self.assertEqual(code, 0)
        self.assertIn("Payment: completed", out)
        self.assertIn("Settlement: not verified by this response", out)
        self.assertNotIn("Order id:", out)
        self.assertNotIn("Charged:", out)
        self.assertNotIn("collection", out.lower())

    def test_a_decline_never_becomes_a_new_order(self):
        with StubService(responses=(DECLINED,)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

        self.assertEqual(code, 0)
        self.assertIn("Payment: declined", out)
        self.assertIn(DECLINED["reason"], out)
        self.assertIn("a fresh quote the user approves", out)
        self.assertNotIn("Order id", out)

    def test_an_expired_approval_link_never_becomes_a_new_order(self):
        expired = REQUIRES_ACTION | {
            "approvalExpiresAtUnixSeconds": str(int(time.time()) - 10)
        }
        with StubService(responses=(expired, COMPLETED)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")
            resumed_code, resumed_out, _ = self.run_command("payment", service.url, "pay_1")
            self.assertEqual(service.calls, [
                ("GET", "/kwal/participant/v1/payments/pay_1"),
                ("GET", "/kwal/participant/v1/payments/pay_1"),
            ])

        self.assertEqual(resumed_code, 0)
        self.assertIn("Order id: order_1", resumed_out)

        self.assertEqual(code, 0)
        self.assertIn("Approval link: expired", out)
        self.assertIn("Do not submit a second checkout", out)
        self.assertIn("read the same payment again", out)
        self.assertNotIn("fresh quote", out)
        self.assertNotIn("Quote: expired", out)

    def test_an_approval_link_is_not_reported_beside_another_state(self):
        self._record()
        waiting = PENDING | {
            "approvalUrl": REQUIRES_ACTION["approvalUrl"],
            "approvalExpiresAtUnixSeconds": str(APPROVAL_EXPIRY),
        }
        with StubService(responses=(waiting,)) as service:
            code, out, _ = self.run_command("payment", service.url)

        self.assertEqual(code, 0)
        self.assertIn("Payment: processing", out)
        self.assertNotIn("Approval link", out)

    def test_a_stopped_payment_asks_for_operator_help(self):
        stopped = {
            "paymentId": "pay_1",
            "state": "PARTICIPANT_PAYMENT_STATE_ERROR",
            "step": "clearing",
            "reason": "The provider did not answer the clearing callback.",
        }
        with StubService(responses=(stopped,)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

        self.assertEqual(code, 0)
        self.assertIn("Payment: stopped for operator help", out)
        self.assertIn("Step: clearing", out)
        self.assertIn("Ask the operator to resolve the recorded outcome", out)

    def test_a_trimmed_payment_id_reaches_one_canonical_route(self):
        with StubService(responses=(COMPLETED,)) as service:
            code, _, _ = self.run_command("payment", service.url, " pay_1 ")

            self.assertEqual(
                service.calls, [("GET", "/kwal/participant/v1/payments/pay_1")]
            )

        self.assertEqual(code, 0)

    def test_a_payment_id_that_cannot_be_one_never_reaches_the_service(self):
        with StubService(responses=(COMPLETED,)) as service:
            code, _, err = self.run_command("payment", service.url, "pay 1")

            self.assertEqual(service.calls, [])

        self.assertEqual(code, 1)
        self.assertIn("without spaces", err)

    def test_a_payment_of_another_participant_is_a_reported_service_error(self):
        with StubService(status=404, payload={"message": "not found"}) as service:
            code, _, err = self.run_command("payment", service.url, "pay_other")

        self.assertEqual(code, 1)
        self.assertIn("failed with HTTP 404", err)
        self.assertIn("Report the service error to the user", err)


if __name__ == "__main__":
    unittest.main()


# A sandbox payment is a simulated swipe of the participant's own card, as the
# service reports it. Amount is the quote in USD; held and charged are vault
# USDC.
SIMULATED_HELD = {
    "paymentId": "pay_1",
    "state": "PARTICIPANT_PAYMENT_STATE_PENDING",
    "step": "card_authorization",
    "cardTransactionId": "600ddd6f-f258-48d0-adf5-d081dd10df04",
    "held": amount("12340000"),
    "amount": {"minorUnits": "1234", "currency": "USD", "decimals": 2},
    "quoteId": "quote_1",
    "reason": "approved; the amount is held on your vault funds",
}

SIMULATED_COMPLETED = SIMULATED_HELD | {
    "state": "PARTICIPANT_PAYMENT_STATE_COMPLETED",
    "step": "card_clearing",
    "held": {},
    "charged": amount("12340000"),
    "reason": "paid from your vault by a simulated card spend; nothing ships",
}

SIMULATED_DECLINED = {
    "paymentId": "pay_1",
    "state": "PARTICIPANT_PAYMENT_STATE_DECLINED",
    "step": "card_authorization",
    "cardTransactionId": "f4d7944a",
    "amount": {"minorUnits": "1234", "currency": "USD", "decimals": 2},
    "reason": "declined by the vault: decline_insufficient_account",
}


class SimulatedCardSpendTests(CommandTests):
    def test_a_simulated_spend_reads_the_card_transaction_and_the_hold(self):
        payment = pws_client.parse_payment(SIMULATED_HELD)

        self.assertTrue(payment.is_simulated_card_spend())
        self.assertEqual(
            payment.card_transaction_id, "600ddd6f-f258-48d0-adf5-d081dd10df04"
        )
        self.assertEqual(str(payment.held), "12.340000 USDC")
        self.assertIsNone(payment.checkout_status)
        self.assertIsNone(payment.approval_url)

    def test_an_agentic_payment_is_not_a_simulated_spend(self):
        self.assertFalse(pws_client.parse_payment(COMPLETED).is_simulated_card_spend())

    def test_a_held_spend_is_polled_then_reported_without_a_second_checkout(self):
        with StubService(responses=(SIMULATED_HELD, SIMULATED_COMPLETED)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

            self.assertEqual(len(service.calls), 2)

        self.assertEqual(code, 0)
        self.assertIn("Payment: completed", out)
        self.assertIn("Charged: 12.340000 USDC", out)
        self.assertIn("Card transaction id: 600ddd6f", out)
        self.assertIn("simulated card spend against the vault", out)
        self.assertIn("nothing ships", out)
        self.assertIn("Settlement: the vault paid the simulated card spend", out)
        self.assertNotIn("Settlement: not verified", out)
        self.assertNotIn("Held on the vault", out)

    def test_a_held_spend_that_stays_pending_reports_the_hold(self):
        with StubService(responses=(SIMULATED_HELD,)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

        self.assertEqual(code, 0)
        self.assertIn("Payment: processing", out)
        self.assertIn("Held on the vault: 12.340000 USDC", out)
        self.assertIn("Do not submit a second checkout", out)

    def test_a_declined_spend_names_the_ledger_reason(self):
        with StubService(responses=(SIMULATED_DECLINED,)) as service:
            code, out, _ = self.run_command("payment", service.url, "pay_1")

        self.assertEqual(code, 0)
        self.assertIn("Payment: declined", out)
        self.assertIn("decline_insufficient_account", out)
        self.assertIn("Do not submit this quote again", out)


class SimulatedCheckoutRefusalTests(CommandTests):
    def test_an_unfunded_checkout_asks_to_fund_the_vault_and_keeps_the_attempt(self):
        refusal = (
            400,
            {
                "type": "tag:kraken.com,2025:ParticipantFundingRequest",
                "rejection": "PARTICIPANT_FUNDING_REJECTION_INSUFFICIENT_FUNDS",
            },
        )
        with StubService(responses=(QUOTE, refusal)) as service:
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

        self.assertEqual(code, 1)
        self.assertIn("the vault cannot cover this quote", out)
        self.assertIn("quote-check quote_1", out)
        self.assertIn("run the same checkout again", out)
        recorded = pws_client.AttemptStore(self.path).find("quote_1")
        self.assertIsNotNone(recorded)

    def test_a_busy_card_waits_for_the_holding_payment_and_keeps_the_attempt(self):
        busy = (
            409,
            {
                "type": "tag:kraken.com,2025:ParticipantCardBusy",
                "data": {"holdingPaymentId": "pay_0"},
            },
        )
        with (
            patch("checkout.new_payment_id", return_value="pay_1"),
            StubService(responses=(QUOTE, busy)) as service,
        ):
            code, out, err = self.run_command("checkout", service.url, "quote_1")

        self.assertEqual(code, 1)
        self.assertIn("another payment holds your card", out)
        self.assertIn("Nothing was saved", out)
        self.assertIn("python3 scripts/register.py payment pay_0", out)
        self.assertIn("run the same checkout again", out)
        self.assertNotIn("Payment id: pay_1", out)
        self.assertNotIn("operator", out + err)
        recorded = pws_client.AttemptStore(self.path).find("quote_1")
        self.assertEqual(recorded.payment_id, "pay_1")

    def test_a_card_busy_with_another_owner_names_no_payment(self):
        busy = (409, {"type": "tag:kraken.com,2025:ParticipantCardBusy"})
        with StubService(responses=(QUOTE, busy)) as service:
            code, out, err = self.run_command("checkout", service.url, "quote_1")

        self.assertEqual(code, 1)
        self.assertIn("another payment holds your card", out)
        self.assertIn("run the same checkout again", out)
        self.assertNotIn("register.py payment", out)
        self.assertNotIn("operator", out + err)

    def test_an_unanswered_checkout_reads_the_recorded_payment_before_a_retry(self):
        for status, body in (
            (503, {"type": "tag:kraken.com,2025:ParticipantUnavailable"}),
            (503, {}),
            (504, {}),
        ):
            with (
                self.subTest(status=status, body=body),
                patch("checkout.new_payment_id", return_value="pay_1"),
                StubService(responses=(QUOTE, (status, body))) as service,
            ):
                pws_client.AttemptStore(self.path).path.unlink(missing_ok=True)
                code, out, err = self.run_command("checkout", service.url, "quote_1")

                self.assertEqual(code, 1)
                self.assertIn("Payment id: pay_1", out)
                self.assertIn("A payment may exist", err)
                self.assertIn('python3 scripts/register.py payment "<payment-id>"', err)
                self.assertIn("ParticipantNotFound", err)
                self.assertIn("run the same checkout again", err)
                self.assertNotIn("register.py status", err)

    def test_a_quote_another_payment_holds_explains_the_repeat_window(self):
        used = {
            "paymentId": "pay_1",
            "state": "PARTICIPANT_PAYMENT_STATE_ERROR",
            "step": "quote_already_used",
            "quoteId": "quote_1",
            "reason": "quote is already attached to another payment; create a new quote",
        }
        with (
            patch("checkout.new_payment_id", return_value="pay_1"),
            StubService(responses=(QUOTE, used)) as service,
        ):
            code, out, _ = self.run_command("checkout", service.url, "quote_1")

        self.assertEqual(code, 0)
        self.assertIn("about 15 minutes", out)
        self.assertIn("Choose another item or quantity", out)
        self.assertNotIn("Ask the operator", out)

    def test_a_paid_quote_read_explains_the_repeat_window(self):
        with StubService(responses=(QUOTE | {"paymentId": "pay_1"},)) as service:
            code, out, _ = self.run_command("quote-check", service.url, "quote_1")

        self.assertEqual(code, 0)
        self.assertIn("Attached payment: pay_1", out)
        self.assertIn("about 15 minutes", out)
