"""Responses must still describe the quote or payment the caller requested.

Every request uses the loopback fixture service and dummy credentials.
"""

import json
import time
from unittest.mock import patch

from support import CommandTests, StubService, amount

import pws_client


QUOTES = "/kwal/participant/v1/quotes"
PAYMENTS = "/kwal/participant/v1/payments"
FUNDING = "/kwal/participant/v1/funding"

QUOTE = {
    "quoteId": "quote_1",
    "lines": [{"variantId": "var_1", "quantity": 1}],
    "shippingOptions": [
        {"shippingOptionId": "ship_standard", "label": "Standard"},
        {"shippingOptionId": "ship_fast", "label": "Fast"},
    ],
    "selectedShippingOptionId": "ship_standard",
    "total": amount("360", currency="USD", decimals=2),
    "expiresAtUnixSeconds": int(time.time()) + 604_800,
}


def ready(quote_id="quote_1"):
    return {
        "state": "PARTICIPANT_FUNDING_STATE_READY",
        "vaultAddress": f"0x{'ab' * 20}",
        "chain": "ink-sepolia",
        "available": amount("4000000"),
        "required": amount("3600000"),
        "quoteFunding": {
            "quoteId": quote_id,
            "merchantTotal": QUOTE["total"],
            "fundingToken": {
                "chainId": "763373",
                "contractAddress": f"0x{'cd' * 20}",
                "symbol": "USDC",
                "decimals": 6,
            },
        },
    }


def payment(*, state="COMPLETED", payment_id="pay_expected", quote_id=None):
    result = {
        "paymentId": payment_id,
        "state": f"PARTICIPANT_PAYMENT_STATE_{state}",
    }
    if quote_id is not None:
        result["quoteId"] = quote_id
    return result


class QuoteResponseValidationTests(CommandTests):
    def test_quote_check_rejects_another_quote_before_reading_its_funding(self):
        with StubService(responses=(QUOTE | {"quoteId": "quote_other"}, ready("quote_other"))) as service:
            code, _, _ = self.run_command("quote-check", service.url, "quote_1")
            self.assertEqual(service.calls, [("GET", f"{QUOTES}/quote_1")])
        self.assertEqual(code, 1)

    def test_shipping_rejects_another_quote_before_skipping_a_reselection(self):
        with StubService(responses=(QUOTE | {"quoteId": "quote_other"}, ready("quote_other"))) as service:
            code, _, _ = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_standard"
            )
            self.assertEqual(service.calls, [("GET", f"{QUOTES}/quote_1")])
        self.assertEqual(code, 1)

    def test_shipping_rejects_a_response_for_another_quote(self):
        changed = QUOTE | {"quoteId": "quote_other", "selectedShippingOptionId": "ship_fast"}
        with StubService(responses=(QUOTE, changed, ready("quote_other"))) as service:
            code, _, _ = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_fast"
            )
            self.assertEqual(service.calls, [
                ("GET", f"{QUOTES}/quote_1"),
                ("POST", f"{QUOTES}/quote_1/shipping"),
            ])
        self.assertEqual(code, 1)

    def test_shipping_rejects_success_that_keeps_the_previous_selection(self):
        with StubService(responses=(QUOTE, QUOTE, ready())) as service:
            code, _, _ = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_fast"
            )
            self.assertEqual(service.calls, [
                ("GET", f"{QUOTES}/quote_1"),
                ("POST", f"{QUOTES}/quote_1/shipping"),
            ])
            self.assertEqual(json.loads(service.bodies[1]), {"shippingOptionId": "ship_fast"})
        self.assertEqual(code, 1)

    def test_shipping_accepts_the_requested_quote_and_selection(self):
        changed = QUOTE | {"selectedShippingOptionId": "ship_fast"}
        with StubService(responses=(QUOTE, changed, ready())) as service:
            code, _, err = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_fast"
            )
            self.assertEqual(service.calls, [
                ("GET", f"{QUOTES}/quote_1"),
                ("POST", f"{QUOTES}/quote_1/shipping"),
                ("GET", f"{FUNDING}?quoteId=quote_1"),
            ])
        self.assertEqual((code, err), (0, ""))

    def test_checkout_rejects_another_quote_before_recording_or_submitting(self):
        with StubService(responses=(QUOTE | {"quoteId": "quote_other"}, payment())) as service:
            code, _, _ = self.run_command("checkout", service.url, "quote_1")
            self.assertEqual(service.calls, [("GET", f"{QUOTES}/quote_1")])
        self.assertEqual(code, 1)
        self.assertIsNone(pws_client.AttemptStore(self.path).latest())


class CheckoutResponseValidationTests(CommandTests):
    def _reject_then_resume(self, mismatched):
        store = pws_client.AttemptStore(self.path)
        with StubService(responses=(QUOTE, mismatched, payment(quote_id="quote_1"))) as service:
            with patch("checkout.new_payment_id", return_value="pay_expected"):
                failed, _, _ = self.run_command("checkout", service.url, "quote_1")
            self.assertEqual(failed, 1)
            saved = store.load()
            self.assertEqual(saved, (pws_client.Attempt(payment_id="pay_expected", quote_id="quote_1"),))
            with patch("checkout.new_payment_id", side_effect=AssertionError("must reuse the saved attempt")):
                code, _, err = self.run_command("checkout", service.url, "quote_1")
            self.assertEqual(service.calls, [
                ("GET", f"{QUOTES}/quote_1"),
                ("POST", PAYMENTS),
                ("POST", PAYMENTS),
            ])
            submitted = [json.loads(body) for body in service.bodies if body]
            self.assertEqual(submitted, [
                {"paymentId": "pay_expected", "quoteId": "quote_1"},
                {"paymentId": "pay_expected", "quoteId": "quote_1"},
            ])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(store.load(), saved)

    def test_checkout_rejects_another_payment_and_preserves_the_attempt(self):
        self._reject_then_resume(payment(payment_id="pay_other", quote_id="quote_1"))

    def test_checkout_rejects_another_quote_and_preserves_the_attempt(self):
        self._reject_then_resume(payment(quote_id="quote_other"))

    def test_checkout_accepts_expected_payment_with_optional_quote_absent(self):
        with StubService(responses=(QUOTE, payment())) as service:
            with patch("checkout.new_payment_id", return_value="pay_expected"):
                code, _, err = self.run_command("checkout", service.url, "quote_1")
            self.assertEqual(service.calls, [("GET", f"{QUOTES}/quote_1"), ("POST", PAYMENTS)])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(pws_client.AttemptStore(self.path).latest().payment_id, "pay_expected")


class PaymentResponseValidationTests(CommandTests):
    def _record(self):
        pws_client.AttemptStore(self.path).record(
            pws_client.Attempt(payment_id="pay_expected", quote_id="quote_1")
        )

    def test_read_rejects_another_payment_before_polling_it(self):
        with StubService(responses=(payment(state="PENDING", payment_id="pay_other"), payment(payment_id="pay_other"))) as service:
            code, _, _ = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")])
        self.assertEqual(code, 1)

    def test_poll_rejects_a_swapped_payment_without_following_its_id(self):
        with StubService(responses=(
            payment(state="PENDING"),
            payment(state="PENDING", payment_id="pay_other"),
            payment(payment_id="pay_other"),
        )) as service:
            code, _, _ = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")] * 2)
        self.assertEqual(code, 1)

    def test_explicit_payment_checks_its_saved_quote_reference(self):
        self._record()
        with StubService(responses=(payment(quote_id="quote_other"),)) as service:
            code, _, _ = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")])
        self.assertEqual(code, 1)

    def test_latest_payment_checks_its_saved_quote_after_an_omitted_reference(self):
        self._record()
        with StubService(responses=(payment(state="PENDING"), payment(quote_id="quote_other"))) as service:
            code, _, _ = self.run_command("payment", service.url)
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")] * 2)
        self.assertEqual(code, 1)

    def test_quote_reference_survives_an_omitted_intermediate_response(self):
        with StubService(responses=(
            payment(state="PENDING", quote_id="quote_1"),
            payment(state="PENDING"),
            payment(quote_id="quote_other"),
        )) as service:
            code, _, _ = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")] * 3)
        self.assertEqual(code, 1)

    def test_quote_reference_first_seen_while_polling_cannot_later_change(self):
        with StubService(responses=(
            payment(state="PENDING"),
            payment(state="PENDING", quote_id="quote_1"),
            payment(quote_id="quote_other"),
        )) as service:
            code, _, _ = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")] * 3)
        self.assertEqual(code, 1)

    def test_consistent_quote_reference_may_appear_later(self):
        self._record()
        with StubService(responses=(payment(state="PENDING"), payment(quote_id="quote_1"))) as service:
            code, _, err = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")] * 2)
        self.assertEqual((code, err), (0, ""))

    def test_optional_quote_reference_can_remain_absent_through_polling(self):
        with StubService(responses=(payment(state="PENDING"), payment())) as service:
            code, _, err = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")] * 2)
        self.assertEqual((code, err), (0, ""))

    def test_consistent_reference_survives_an_omitted_intermediate_response(self):
        with StubService(responses=(
            payment(state="PENDING", quote_id="quote_1"),
            payment(state="PENDING"),
            payment(quote_id="quote_1"),
        )) as service:
            code, _, err = self.run_command("payment", service.url, "pay_expected")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_expected")] * 3)
        self.assertEqual((code, err), (0, ""))

    def test_explicit_unknown_payment_can_be_read_with_an_unreadable_local_record(self):
        pws_client.AttemptStore(self.path).path.write_text("{", encoding="utf-8")
        with StubService(responses=(payment(payment_id="pay_external", quote_id="quote_external"),)) as service:
            code, _, err = self.run_command("payment", service.url, "pay_external")
            self.assertEqual(service.calls, [("GET", f"{PAYMENTS}/pay_external")])
        self.assertEqual((code, err), (0, ""))
