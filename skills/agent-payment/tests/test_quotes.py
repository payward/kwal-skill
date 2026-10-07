import json
import time
import unittest
import unittest.mock
from typing import Any

from support import TOKEN, CommandTests, StubService, amount

import pws_client  # noqa: E402
import register  # noqa: E402
from pws_client import ConfigurationError, ServiceError  # noqa: E402

# A command test reads the real clock, so a valid quote stays ahead of it.
EXPIRY = int(time.time()) + 604_800

ADDRESS = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "phone": "+447700900123",
    "line1": "1 Test Street",
    "line2": "Flat 2",
    "city": "London",
    "region": "Greater London",
    "postalCode": "E1 6AN",
    "country": "GB",
}

LINES = (pws_client.QuoteLine(variant_id="var_1", quantity=2),)

SHIPPING_OPTION = {
    "shippingOptionId": "ship_standard",
    "label": "Standard",
    "price": amount("50", currency="USD", decimals=2),
}

QUOTE = {
    "quoteId": "quote_1",
    "lines": [{"variantId": "var_1", "quantity": 2}],
    "shippingOptions": [SHIPPING_OPTION],
    "selectedShippingOptionId": "ship_standard",
    "subtotal": amount("300", currency="USD", decimals=2),
    "shipping": amount("50", currency="USD", decimals=2),
    "tax": amount("10", currency="USD", decimals=2),
    "total": amount("360", currency="USD", decimals=2),
    "expiresAtUnixSeconds": str(EXPIRY),
}

DIGITAL_QUOTE = {
    "quoteId": "quote_2",
    "lines": [{"variantId": "var_1", "quantity": 1}],
    "total": amount("150", currency="USD", decimals=2),
    "expiresAtUnixSeconds": EXPIRY,
}


class ShippingAddressTests(unittest.TestCase):
    def test_no_flag_sends_no_address(self):
        self.assertIsNone(
            pws_client.normalize_shipping_address({key: None for key in ADDRESS})
        )

    def test_a_stated_address_reaches_the_service_as_the_contract_states_it(self):
        self.assertEqual(
            pws_client.normalize_shipping_address(
                ADDRESS | {"region": " Greater London "}
            ),
            ADDRESS,
        )

    def test_an_address_without_optional_fields_is_accepted(self):
        required = {
            key: value
            for key, value in ADDRESS.items()
            if key not in ("line2", "region", "postalCode")
        }
        self.assertEqual(pws_client.normalize_shipping_address(required), required)

    def test_a_partial_address_names_the_flags_it_needs(self):
        with self.assertRaises(ConfigurationError) as refusal:
            pws_client.normalize_shipping_address(
                ADDRESS | {"phone": None, "country": None}
            )
        self.assertIn("--phone --country", str(refusal.exception))


class QuoteParseTests(unittest.TestCase):
    def test_a_quote_reports_every_amount_the_user_reviews(self):
        quote = pws_client.parse_quote(QUOTE)
        self.assertEqual(quote.quote_id, "quote_1")
        self.assertEqual(quote.lines, LINES)
        self.assertEqual(str(quote.subtotal), "3.00 USD")
        self.assertEqual(str(quote.shipping), "0.50 USD")
        self.assertEqual(str(quote.tax), "0.10 USD")
        self.assertEqual(str(quote.total), "3.60 USD")
        self.assertEqual(quote.expires_at, EXPIRY)
        self.assertEqual(quote.selected_shipping_option_id, "ship_standard")
        self.assertEqual(str(quote.shipping_options[0].price), "0.50 USD")
        self.assertEqual(quote.shipping_options[0].label, "Standard")

    def test_a_quote_without_shipping_reports_only_its_total(self):
        quote = pws_client.parse_quote(DIGITAL_QUOTE)
        self.assertEqual(quote.shipping_options, ())
        self.assertIsNone(quote.selected_shipping_option_id)
        self.assertIsNone(quote.subtotal)
        self.assertIsNone(quote.tax)
        self.assertFalse(quote.needs_shipping_selection())

    def test_an_offered_shipping_option_waits_for_a_selection(self):
        quote = pws_client.parse_quote(
            {key: value for key, value in QUOTE.items()
             if key != "selectedShippingOptionId"}
        )
        self.assertTrue(quote.needs_shipping_selection())

    def test_a_quote_expires_at_the_instant_the_service_states(self):
        quote = pws_client.parse_quote(QUOTE)
        self.assertFalse(quote.is_expired(EXPIRY - 1))
        self.assertTrue(quote.is_expired(EXPIRY))

    def test_a_quote_refuses_a_body_it_cannot_review(self):
        cases = {
            "not an object": [],
            "no quote id": QUOTE | {"quoteId": None},
            "no total": QUOTE | {"total": None},
            "zero total": QUOTE | {"total": amount("0")},
            "no lines": QUOTE | {"lines": []},
            "line without a quantity": QUOTE | {"lines": [{"variantId": "var_1"}]},
            "line quantity zero": QUOTE
            | {"lines": [{"variantId": "var_1", "quantity": 0}]},
            "line without a variant": QUOTE | {"lines": [{"quantity": 1}]},
            "shipping option without a label": QUOTE
            | {"shippingOptions": [{"shippingOptionId": "ship_standard"}]},
            "selection not offered": QUOTE | {"selectedShippingOptionId": "ship_fast"},
            "no expiry": DIGITAL_QUOTE | {"expiresAtUnixSeconds": None},
            "zero expiry": DIGITAL_QUOTE | {"expiresAtUnixSeconds": "0"},
            "expiry not an instant": DIGITAL_QUOTE | {"expiresAtUnixSeconds": "soon"},
            "expiry beyond the contract range": DIGITAL_QUOTE
            | {"expiresAtUnixSeconds": 2**70},
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ServiceError):
                    pws_client.parse_quote(body)


class QuoteServiceTests(unittest.TestCase):
    def test_a_quote_request_carries_the_lines_and_the_address(self):
        with StubService(responses=(QUOTE,)) as service:
            quote = pws_client.create_quote(
                service.url,
                TOKEN,
                LINES,
                email="buyer@example.com",
                shipping_address=pws_client.normalize_shipping_address(ADDRESS),
            )
            self.assertEqual(service.calls, [("POST", pws_client.QUOTES_PATH)])
            self.assertEqual(service.authorizations, [f"Bearer {TOKEN}"])
            body = json.loads(service.bodies[0])
        self.assertEqual(body["lines"], [{"variantId": "var_1", "quantity": 2}])
        self.assertEqual(body["shippingAddress"], ADDRESS)
        self.assertEqual(body["email"], "buyer@example.com")
        self.assertEqual(quote.quote_id, "quote_1")

    def test_a_quote_without_an_address_sends_no_address(self):
        with StubService(responses=(DIGITAL_QUOTE,)) as service:
            pws_client.create_quote(service.url, TOKEN, LINES, email="buyer@example.com")
            self.assertNotIn("shippingAddress", json.loads(service.bodies[0]))

    def test_a_quote_read_escapes_the_identifier_in_the_path(self):
        with StubService(responses=(QUOTE | {"quoteId": "quote/1"},)) as service:
            quote = pws_client.read_quote(service.url, TOKEN, "quote/1")
            self.assertEqual(
                service.calls, [("GET", f"{pws_client.QUOTES_PATH}/quote%2F1")]
            )
        self.assertEqual(quote.quote_id, "quote/1")

    def test_a_shipping_selection_returns_the_repriced_quote(self):
        with StubService(responses=(QUOTE,)) as service:
            quote = pws_client.select_shipping(
                service.url, TOKEN, "quote_1", "ship_standard"
            )
            self.assertEqual(
                service.calls,
                [("POST", f"{pws_client.QUOTES_PATH}/quote_1/shipping")],
            )
            self.assertEqual(
                json.loads(service.bodies[0]),
                {"shippingOptionId": "ship_standard"},
            )
        self.assertEqual(str(quote.total), "3.60 USD")

    def test_a_response_that_repeats_the_session_token_is_refused(self):
        with StubService(responses=(QUOTE | {"quoteId": TOKEN},)) as service:
            with self.assertRaises(ServiceError):
                pws_client.read_quote(service.url, TOKEN, "quote_1")


TOKEN_IDENTITY = {
    "chainId": "763373",
    "contractAddress": f"0x{'cd' * 20}",
    "symbol": "USDC",
    "decimals": 6,
}
READY = {
    "state": "PARTICIPANT_FUNDING_STATE_READY",
    "vaultAddress": f"0x{'ab' * 20}",
    "chain": "ink-sepolia",
    "available": amount("4000000"),
    "quoteFunding": {
        "quoteId": "quote_1",
        "merchantTotal": QUOTE["total"],
        "fundingToken": TOKEN_IDENTITY,
    },
    "required": amount("3600000"),
}


def quoted(**fields: Any) -> dict[str, Any]:
    return {"quoteFunding": READY["quoteFunding"] | fields}


PROCESSING = {key: value for key, value in READY.items() if key != "available"} | {
    "state": "PARTICIPANT_FUNDING_STATE_PROCESSING"
}


class QuoteCommandTests(CommandTests):
    def test_sandbox_quote_labels_demo_pricing_and_checks_the_saved_total(self):
        quote_id = "sandbox_quote_123"
        total = {"minorUnits": "2", "currency": "USD"}
        quote = {
            "quoteId": quote_id,
            "lines": [{"variantId": "var_1", "quantity": 2}],
            "total": total,
            "expiresAtUnixSeconds": EXPIRY,
        }
        funding = (
            READY
            | quoted(quoteId=quote_id, merchantTotal=total)
            | {"required": amount("2000000")}
        )
        with StubService(responses=(quote, funding)) as service:
            code, out, err = self.run_command("quote-check", service.url, quote_id)
            self.assertEqual(service.calls, [
                ("GET", f"{pws_client.QUOTES_PATH}/{quote_id}"),
                ("GET", f"{pws_client.FUNDING_PATH}?quoteId={quote_id}"),
            ])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Sandbox quote: fixed demo pricing of 1 USDC per item", out)
        self.assertIn("No merchant order or shipping", out)
        self.assertIn("Demo total: 2 USD", out)
        self.assertIn("Required funding: 2.000000 USDC", out)
        self.assertIn("Card-spendable: 4.000000 USDC", out)
        self.assertIn("Withdrawable: unknown (operator required)", out)
        self.assertNotIn("Merchant total:", out)
        self.assertIn(f"Next: hand quote id {quote_id}", out)

    def test_blocked_or_processing_funding_never_hands_off_to_checkout(self):
        for state in ("ERROR", "PROCESSING"):
            funding = PROCESSING | {"state": f"PARTICIPANT_FUNDING_STATE_{state}"}
            with self.subTest(state=state), StubService(responses=(QUOTE, funding)) as service:
                code, out, err = self.run_command("quote-check", service.url, "quote_1")
            self.assertEqual((code, err), (0, ""))
            self.assertNotIn("Next: hand quote id", out)
            self.assertNotIn("Next: transfer", out)
            self.assertNotIn("Card-spendable:", out)
            self.assertIn("Withdrawable: unknown (operator required)", out)
            self.assertIn("operator" if state == "ERROR" else "Funding: processing", out)

    def test_the_quote_command_prices_the_readiness_for_the_total(self):
        with StubService(responses=(QUOTE, READY)) as service:
            code, out, err = self.run_command(
                "quote",
                service.url,
                "--variant",
                "var_1",
                "--quantity",
                "2",
                "--email",
                "buyer@example.com",
                "--first-name",
                "Ada",
                "--last-name",
                "Lovelace",
                "--phone",
                "+447700900123",
                "--line1",
                "1 Test Street",
                "--line2",
                "Flat 2",
                "--city",
                "London",
                "--region",
                "Greater London",
                "--postal-code",
                "E1 6AN",
                "--country",
                "GB",
            )
            self.assertEqual(
                service.calls,
                [
                    ("POST", pws_client.QUOTES_PATH),
                    ("GET", f"{pws_client.FUNDING_PATH}?quoteId=quote_1"),
                ],
            )
            body = json.loads(service.bodies[0])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(body["lines"], [{"variantId": "var_1", "quantity": 2}])
        self.assertEqual(body["shippingAddress"], ADDRESS)
        self.assertEqual(body["email"], "buyer@example.com")
        self.assertIn("Item var_1 x2", out)
        self.assertIn("Total: 3.60 USD", out)
        self.assertIn(f"Quote expires at: {EXPIRY}", out)
        self.assertIn("Funding: ready for payment", out)
        self.assertIn("Next: hand quote id quote_1", out)

    def test_a_quote_without_address_flags_sends_no_address(self):
        funding = (
            READY
            | quoted(quoteId="quote_2", merchantTotal=DIGITAL_QUOTE["total"])
            | {"required": amount("1500000")}
        )
        with StubService(responses=(DIGITAL_QUOTE, funding)) as service:
            code, out, _ = self.run_command(
                "quote", service.url, "--email", "buyer@example.com", "--variant", "var_1"
            )
            body = json.loads(service.bodies[0])
            self.assertNotIn("shippingAddress", body)
            self.assertEqual(body["email"], "buyer@example.com")
            self.assertEqual(body["lines"], [{"variantId": "var_1", "quantity": 1}])
        self.assertEqual(code, 0)
        self.assertIn("Total: 1.50 USD", out)

    def test_a_quote_the_provider_needs_an_address_for_asks_for_one(self):
        refusal = {
            "type": "tag:kraken.com,2025:ParticipantBadRequest",
            "data": {"reason": "PARTICIPANT_BAD_REQUEST_REASON_SHIPPING_ADDRESS_REQUIRED"},
        }
        with StubService(responses=((400, refusal),)) as service:
            code, _, err = self.run_command(
                "quote", service.url, "--email", "buyer@example.com", "--variant", "var_1"
            )
            self.assertEqual(service.calls, [("POST", pws_client.QUOTES_PATH)])
        self.assertEqual(code, 1)
        self.assertIn("bad_request_reason=SHIPPING_ADDRESS_REQUIRED", err)
        self.assertIn("needs a shipping address", err)
        self.assertNotIn("Correct the command input", err)

    def test_a_partial_address_stops_before_the_service(self):
        with StubService(responses=(QUOTE,)) as service:
            code, _, err = self.run_command(
                "quote", service.url,
                "--email", "buyer@example.com",
                "--variant", "var_1", "--first-name", "Ada",
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Shipping address needs", err)
        self.assertIn("action: Correct the command input", err)

    def test_missing_or_empty_buyer_email_stops_before_the_service(self):
        for email_flags in ((), ("--email", " ")):
            with self.subTest(email_flags=email_flags):
                with StubService(responses=(QUOTE,)) as service:
                    code, _, err = self.run_command(
                        "quote", service.url, "--variant", "var_1", *email_flags
                    )
                    self.assertEqual(service.calls, [])
                self.assertEqual(code, 1)
                self.assertIn("email", err)
                self.assertIn("action: Correct the command input", err)

    def test_a_quantity_the_service_cannot_price_stops_before_the_service(self):
        with StubService(responses=(QUOTE,)) as service:
            code, _, err = self.run_command(
                "quote", service.url,
                "--email", "buyer@example.com",
                "--variant", "var_1", "--quantity", "101",
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Quantity must be between 1 and 100.", err)
        self.assertIn("action: Correct the command input", err)

    def test_an_offered_shipping_option_stops_before_the_funding_check(self):
        unselected = {
            key: value
            for key, value in QUOTE.items()
            if key != "selectedShippingOptionId"
        }
        with StubService(responses=(unselected,)) as service:
            code, out, _ = self.run_command("quote-check", service.url, "quote_1")
            self.assertEqual(
                service.calls, [("GET", f"{pws_client.QUOTES_PATH}/quote_1")]
            )
        self.assertEqual(code, 0)
        self.assertIn("Shipping option ship_standard | Standard | 0.50 USD", out)
        self.assertIn("Shipping: selection required", out)
        self.assertIn("Next: select one printed shipping option", out)

    def test_an_expired_quote_asks_for_a_fresh_one(self):
        with StubService(responses=(QUOTE | {"expiresAtUnixSeconds": "1"},)) as service:
            code, out, _ = self.run_command("quote-check", service.url, "quote_1")
            self.assertEqual(len(service.calls), 1)
        self.assertEqual(code, 0)
        self.assertIn("Quote: expired", out)
        self.assertIn("Do not reuse this quote id.", out)

    def test_a_quote_that_expires_during_the_funding_wait_is_not_carried(self):
        clock = iter((EXPIRY - 10,))
        with StubService(responses=(QUOTE, PROCESSING, READY)) as service:
            with unittest.mock.patch.object(
                register.time, "time", lambda: next(clock, EXPIRY + 10)
            ):
                code, out, _ = self.run_command(
                    "quote-check", service.url, "quote_1", expires_at=EXPIRY + 604_800
                )
            funding = [
                call for call in service.calls if pws_client.FUNDING_PATH in call[1]
            ]
        self.assertEqual(code, 0)
        priced = f"{pws_client.FUNDING_PATH}?quoteId=quote_1"
        self.assertEqual(funding, [("GET", priced), ("GET", priced)])
        self.assertIn("Quote: expired", out)
        self.assertIn("Do not reuse this quote id.", out)
        self.assertNotIn("Next: hand quote id", out)

    def test_a_funding_refusal_after_expiry_asks_for_a_fresh_quote(self):
        clock = iter((EXPIRY - 10,))
        with StubService(
            responses=(QUOTE, (400, {"message": "quote has expired"}))
        ) as service:
            with unittest.mock.patch.object(
                register.time, "time", lambda: next(clock, EXPIRY + 10)
            ):
                code, out, err = self.run_command(
                    "quote-check", service.url, "quote_1", expires_at=EXPIRY + 604_800
                )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Quote: expired", out)
        self.assertIn("Do not reuse this quote id.", out)
        self.assertNotIn("Next: hand quote id", out)

    def test_a_usd_quote_checks_usdc_funding_and_reports_the_shortfall(self):
        quote = DIGITAL_QUOTE | {"total": amount("1902", currency="USD", decimals=2)}
        shortfall = (
            READY
            | quoted(quoteId="quote_2", merchantTotal=quote["total"])
            | {
                "required": amount("19020000"),
                "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
                "available": amount("18000000"),
                "shortfall": amount("1020000"),
            }
        )
        with StubService(responses=(quote, shortfall)) as service:
            code, out, err = self.run_command("quote-check", service.url, "quote_2")
            self.assertEqual(
                service.calls,
                [
                    ("GET", f"{pws_client.QUOTES_PATH}/quote_2"),
                    ("GET", f"{pws_client.FUNDING_PATH}?quoteId=quote_2"),
                ],
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Total: 19.02 USD", out)
        self.assertIn("Required funding: 19.020000 USDC", out)
        self.assertIn("Shortfall: 1.020000 USDC", out)
        self.assertIn("Funding: more funds needed", out)
        self.assertNotIn("Next: hand quote id", out)

    def test_backend_owns_the_quote_funding_currency_policy(self):
        quote = DIGITAL_QUOTE | {"total": amount("1902", currency="EUR", decimals=2)}
        with StubService(responses=(quote, (400, {"message": "unsupported currency"}))) as service:
            code, _, _ = self.run_command("quote-check", service.url, "quote_2")
            self.assertEqual(service.calls, [
                ("GET", f"{pws_client.QUOTES_PATH}/quote_2"),
                ("GET", f"{pws_client.FUNDING_PATH}?quoteId=quote_2"),
            ])
        self.assertEqual(code, 1)

    def test_updated_backend_amounts_require_a_new_price_review(self):
        updated = (
            READY
            | quoted(merchantTotal=amount("1902", currency="USD", decimals=2))
            | {"required": amount("19020000"), "available": amount("20000000")}
        )
        with StubService(responses=(QUOTE, updated)) as service:
            code, out, err = self.run_command("quote-check", service.url, "quote_1")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Merchant total: 19.02 USD", out)
        self.assertIn("Required funding: 19.020000 USDC", out)
        self.assertIn("quote price changed", out)
        self.assertNotIn("Next: hand quote id", out)

    def test_missing_or_inconsistent_quote_funding_metadata_is_refused(self):
        for body in (
            READY | quoted(quoteId="quote_other"),
            READY | quoted(quoteId=None),
            READY | quoted(merchantTotal=None),
            READY | {"required": None},
            READY | quoted(fundingToken=None),
            READY | quoted(fundingToken=TOKEN_IDENTITY | {"symbol": "USDT"}),
            READY | quoted(fundingToken=TOKEN_IDENTITY | {"chainId": "0"}),
            READY | {"required": {"currency": "USDC", "decimals": 6}},
            READY | quoted(merchantTotal={"currency": "USD", "decimals": 2}),
            READY | {"available": amount("1000000")},
            READY
            | {
                "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
                "available": amount("4000000"),
                "shortfall": amount("1000000"),
            },
            READY
            | {
                "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
                "available": amount("2500000"),
                "shortfall": amount("2000000"),
            },
        ):
            with self.subTest(body=body):
                with StubService(responses=(QUOTE, body)) as service:
                    code, out, _ = self.run_command("quote-check", service.url, "quote_1")
                self.assertEqual(code, 1)
                self.assertNotIn("Next: hand quote id", out)

    def test_a_balance_in_another_currency_is_not_compared_to_the_total(self):
        balance = dict(READY, available=amount("4000000", currency="USDT"))
        with StubService(responses=(QUOTE, balance)) as service:
            code, _, err = self.run_command("quote-check", service.url, "quote_1")
        self.assertEqual(code, 1)
        self.assertIn("Funding response amounts must use USDC with 6 decimals", err)
        self.assertIn("action: Report the service error", err)

    def test_the_shipping_command_reports_the_repriced_quote(self):
        express = SHIPPING_OPTION | {"shippingOptionId": "ship_express", "label": "Express"}
        previous = QUOTE | {
            "shippingOptions": [SHIPPING_OPTION, express],
            "selectedShippingOptionId": "ship_express",
        }
        with StubService(responses=(previous, QUOTE, READY)) as service:
            code, out, err = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_standard"
            )
            self.assertEqual(
                service.calls,
                [
                    ("GET", f"{pws_client.QUOTES_PATH}/quote_1"),
                    ("POST", f"{pws_client.QUOTES_PATH}/quote_1/shipping"),
                    (
                        "GET",
                        f"{pws_client.FUNDING_PATH}?quoteId=quote_1",
                    ),
                ],
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Shipping: 0.50 USD", out)
        self.assertIn("Total: 3.60 USD", out)
        self.assertIn("Funding: ready for payment", out)

    def test_reselecting_the_current_shipping_option_does_not_post_again(self):
        with StubService(responses=(QUOTE, READY)) as service:
            code, out, err = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_standard"
            )
            self.assertEqual(service.calls, [
                ("GET", f"{pws_client.QUOTES_PATH}/quote_1"),
                ("GET", f"{pws_client.FUNDING_PATH}?quoteId=quote_1"),
            ])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Selected shipping option: ship_standard", out)
        self.assertIn("Total: 3.60 USD", out)
        self.assertIn("Funding: ready for payment", out)

    def test_reselection_of_an_expired_quote_stops_before_funding(self):
        expired = QUOTE | {"expiresAtUnixSeconds": 1}
        with StubService(responses=(expired,)) as service:
            code, out, err = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_standard"
            )
            self.assertEqual(service.calls, [("GET", f"{pws_client.QUOTES_PATH}/quote_1")])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Quote: expired", out)
        self.assertNotIn("Funding: ready", out)

    def test_a_rejected_shipping_change_stops_and_directs_the_caller_to_read(self):
        unselected = {k: v for k, v in QUOTE.items() if k != "selectedShippingOptionId"}
        refusal = {"type": "tag:kraken.com,2025:ParticipantBadRequest"}
        with StubService(responses=(unselected, (400, refusal))) as service:
            code, _, err = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_standard"
            )
            self.assertEqual(service.calls, [
                ("GET", f"{pws_client.QUOTES_PATH}/quote_1"),
                ("POST", f"{pws_client.QUOTES_PATH}/quote_1/shipping"),
            ])
        self.assertEqual(code, 1)
        self.assertIn("service_error=ParticipantBadRequest", err)
        self.assertIn("quote-check <quote-id>", err)
        self.assertIn("Do not retry the same rejected selection", err)
        self.assertNotIn("register.py status", err)

    def test_a_shortfall_directs_the_user_to_the_deposit(self):
        shortfall = READY | {
            "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
            "available": amount("2500000"),
            "shortfall": amount("1100000"),
        }
        with StubService(responses=(QUOTE, shortfall)) as service:
            code, out, _ = self.run_command("quote-check", service.url, "quote_1")
        self.assertEqual(code, 0)
        self.assertIn("Funding: more funds needed", out)
        self.assertIn("1.100000 USDC", out)

    def test_a_shortfall_in_other_token_units_is_not_compared_to_the_total(self):
        shortfall = READY | {
            "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
            "available": amount("2500000"),
            "shortfall": amount("110", decimals=2),
        }
        with StubService(responses=(QUOTE, shortfall)) as service:
            code, _, err = self.run_command("quote-check", service.url, "quote_1")
        self.assertEqual(code, 1)
        self.assertIn("Funding response amounts must use USDC with 6 decimals", err)
        self.assertIn("action: Report the service error", err)

    def test_a_quote_the_helper_cannot_report_asks_for_operator_help(self):
        with StubService(responses=({"quoteId": "quote_1"},)) as service:
            code, _, err = self.run_command("quote-check", service.url, "quote_1")
        self.assertEqual(code, 1)
        self.assertIn("Quote response reports no total.", err)
        self.assertIn("action: Report the service error", err)

RESERVED_QUOTE = QUOTE | {"paymentId": "pay_1"}


class ReservedQuoteTests(CommandTests):
    def _assert_resumes(self, out: str) -> None:
        self.assertIn("Attached payment: pay_1", out)
        self.assertIn("Next: read payment pay_1 with the payment command.", out)
        self.assertNotIn("Next: hand quote id", out)

    def test_a_reserved_quote_stops_before_the_funding_check(self):
        with StubService(responses=(RESERVED_QUOTE,)) as service:
            code, out, err = self.run_command("quote-check", service.url, "quote_1")
            self.assertEqual(
                service.calls, [("GET", f"{pws_client.QUOTES_PATH}/quote_1")]
            )
        self.assertEqual((code, err), (0, ""))
        self._assert_resumes(out)

    def test_a_reserved_quote_resumes_the_payment_before_any_other_refusal(self):
        unselected = {
            key: value
            for key, value in RESERVED_QUOTE.items()
            if key != "selectedShippingOptionId"
        }
        expired = RESERVED_QUOTE | {"expiresAtUnixSeconds": "1"}
        for name, quote in (("expired", expired), ("unselected", unselected)):
            with self.subTest(quote=name):
                with StubService(responses=(quote,)) as service:
                    code, out, _ = self.run_command(
                        "quote-check", service.url, "quote_1"
                    )
                self.assertEqual(code, 0)
                self._assert_resumes(out)
                self.assertNotIn("Quote: expired", out)
                self.assertNotIn("Shipping: selection required", out)

    def test_a_reserved_quote_is_never_repriced(self):
        with StubService(responses=(RESERVED_QUOTE,)) as service:
            code, out, _ = self.run_command(
                "shipping", service.url, "quote_1", "--option", "ship_standard"
            )
            self.assertEqual(
                service.calls, [("GET", f"{pws_client.QUOTES_PATH}/quote_1")]
            )
        self.assertEqual(code, 0)
        self._assert_resumes(out)


if __name__ == "__main__":
    unittest.main()
