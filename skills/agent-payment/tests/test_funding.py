import unittest
import unittest.mock

from support import TOKEN, CommandTests, NoSleep, StubService, amount

import pws_client  # noqa: E402
from pws_client import ConfigurationError, ServiceError  # noqa: E402

VAULT = f"0x{'ab' * 20}"

VAULT_FIELDS = {"vaultAddress": VAULT, "chain": "ink-sepolia"}

READY = VAULT_FIELDS | {
    "state": "PARTICIPANT_FUNDING_STATE_READY",
    "available": amount("4000000"),
}
FUNDS_NEEDED = VAULT_FIELDS | {
    "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
    "available": amount("2500000"),
    "shortfall": amount("1500000"),
    "required": amount("4000000"),
}
PROCESSING = VAULT_FIELDS | {"state": "PARTICIPANT_FUNDING_STATE_PROCESSING"}
SETUP_NEEDED = {"state": "PARTICIPANT_FUNDING_STATE_SETUP_NEEDED"}


class UnitDisplayTests(unittest.TestCase):
    def test_minor_units_are_shown_in_whole_units(self) -> None:
        cases = {
            (0, 6): "0.000000",
            (1_500_000, 6): "1.500000",
            (4_000_000, 6): "4.000000",
            (5, 2): "0.05",
            (7, 0): "7",
            (123_456_789, 6): "123.456789",
        }
        for (units, decimals), shown in cases.items():
            with self.subTest(units=units, decimals=decimals):
                self.assertEqual(pws_client.format_units(units, decimals), shown)


class RequiredAmountTests(unittest.TestCase):
    def test_a_required_amount_keeps_its_own_value(self) -> None:
        self.assertEqual(
            pws_client.normalize_required_minor_units(" 0001500000 "), "1500000"
        )

    def test_an_unusable_required_amount_is_refused(self) -> None:
        cases = {
            "empty": "   ",
            "whole units": "1.50",
            "negative": "-1",
            "not a number": "some",
            "wider than the token": str(2**256),
            "digits of another script": "\u0661\u0665\u0660\u0660",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ConfigurationError):
                    pws_client.normalize_required_minor_units(value)


class FundingParseTests(unittest.TestCase):
    def test_a_shortfall_state_carries_the_amounts_and_the_vault(self) -> None:
        funding = pws_client.parse_funding(FUNDS_NEEDED)
        self.assertEqual(funding.state, "funds_needed")
        self.assertEqual(funding.vault_address, VAULT)
        self.assertEqual(funding.chain, "ink-sepolia")
        self.assertEqual(str(funding.available), "2.500000 USDC")
        self.assertEqual(str(funding.shortfall), "1.500000 USDC")

    def test_a_ready_state_carries_no_shortfall(self) -> None:
        funding = pws_client.parse_funding(READY)
        self.assertEqual(funding.state, "ready")
        self.assertEqual(funding.available.minor_units, 4_000_000)
        self.assertIsNone(funding.shortfall)

    def test_a_ready_state_accepts_a_reported_zero_shortfall(self) -> None:
        body = READY | {"shortfall": {"currency": "USDC", "decimals": 6}}
        self.assertIsNone(pws_client.parse_funding(body).shortfall)

    def test_an_omitted_zero_is_read_as_that_zero(self) -> None:
        body = {
            "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
            "available": {"currency": "USDC", "decimals": 6},
        }
        self.assertEqual(str(pws_client.parse_funding(body).available), "0.000000 USDC")


    def test_an_amount_without_its_currency_carries_nothing_to_report(self) -> None:
        # The currency is the only field proto3 JSON always states for a
        # reported amount, so its absence means no amount was reported.
        body = PROCESSING | {"available": {"minorUnits": "4000000", "decimals": 6}}
        self.assertIsNone(pws_client.parse_funding(body).available)

    def test_an_omitted_amount_carries_nothing_to_report(self) -> None:
        funding = pws_client.parse_funding(PROCESSING | {"available": {}})
        self.assertIsNone(funding.available)
        self.assertIsNone(funding.shortfall)

    def test_a_processing_state_carries_the_vault_it_watches(self) -> None:
        funding = pws_client.parse_funding(PROCESSING)
        self.assertEqual(funding.state, "processing")
        self.assertEqual(funding.vault_address, VAULT)
        self.assertIsNone(funding.available)

    def test_a_setup_needed_state_carries_no_vault(self) -> None:
        funding = pws_client.parse_funding(SETUP_NEEDED)
        self.assertEqual(funding.state, "setup_needed")
        self.assertIsNone(funding.vault_address)



class FundingServiceTests(NoSleep):
    def test_a_readiness_read_reserves_nothing_and_sends_no_body(self) -> None:
        with StubService(responses=(READY,)) as service:
            funding = pws_client.read_funding(service.url, TOKEN)
            self.assertEqual(service.calls, [("GET", pws_client.FUNDING_PATH)])
            self.assertEqual(service.bodies, [b""])
            self.assertEqual(service.authorizations, [f"Bearer {TOKEN}"])
        self.assertEqual(funding.state, "ready")

    def test_a_required_amount_travels_with_the_read(self) -> None:
        with StubService(responses=(FUNDS_NEEDED,)) as service:
            pws_client.read_funding(
                service.url, TOKEN, required_minor_units="4000000"
            )
            self.assertEqual(
                service.calls,
                [("GET", f"{pws_client.FUNDING_PATH}?requiredMinorUnits=4000000")],
            )

    def test_a_quote_and_a_stated_amount_are_never_sent_together(self) -> None:
        with StubService(responses=(READY,)) as service:
            with self.assertRaises(ConfigurationError):
                pws_client.read_funding(
                    service.url,
                    TOKEN,
                    required_minor_units="4000000",
                    quote_id="quote_1",
                )
            self.assertEqual(service.calls, [])

    def test_polling_is_bounded_and_requests_no_second_deposit(self) -> None:
        with StubService(responses=(PROCESSING,)) as service:
            funding = pws_client.poll_funding(
                service.url,
                TOKEN,
                pws_client.parse_funding(PROCESSING),
                attempts=4,
            )
            self.assertEqual(len(service.calls), 4)
            self.assertNotIn(("POST", pws_client.FUNDING_PATH), service.calls)
        self.assertEqual(funding.state, "processing")
        self.assertEqual(
            self.sleep.call_args_list,
            [unittest.mock.call(pws_client.POLL_INTERVAL_SECONDS)] * 4,
        )

    def test_polling_keeps_the_required_amount_on_every_read(self) -> None:
        with StubService(responses=(
            PROCESSING | {"required": amount("4000000")},
            READY | {"required": amount("4000000")},
        )) as service:
            funding = pws_client.poll_funding(
                service.url,
                TOKEN,
                pws_client.parse_funding(PROCESSING),
                required_minor_units="4000000",
                attempts=4,
            )
            self.assertEqual(
                service.calls,
                [("GET", f"{pws_client.FUNDING_PATH}?requiredMinorUnits=4000000")] * 2,
            )
        self.assertEqual(funding.state, "ready")

    def test_a_settled_state_is_returned_without_a_read(self) -> None:
        with StubService(responses=(READY,)) as service:
            funding = pws_client.poll_funding(
                service.url, TOKEN, pws_client.parse_funding(READY)
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(funding.state, "ready")
        self.sleep.assert_not_called()

    def test_a_reflected_token_is_never_returned_for_display(self) -> None:
        with StubService(
            responses=(FUNDS_NEEDED | {"chain": f"ink-sepolia {TOKEN}"},)
        ) as service:
            funding = pws_client.read_funding(service.url, TOKEN, required_minor_units="4000000")
        self.assertNotIn(TOKEN, funding.chain)
        self.assertIn("[redacted]", funding.chain)


    def test_a_reflected_token_in_an_address_is_refused(self) -> None:
        hex_token = "ab" * 8
        body = FUNDS_NEEDED | {"vaultAddress": f"0x{hex_token}{'cd' * 12}"}
        with StubService(responses=(body,)) as service:
            with self.assertRaises(ServiceError) as raised:
                pws_client.read_funding(
                    service.url, hex_token, required_minor_units="4000000"
                )
        self.assertNotIn(hex_token, str(raised.exception))
        self.assertIn("session token in an address", str(raised.exception))

    def test_a_service_error_hides_the_service_detail(self) -> None:
        with StubService(
            responses=((503, {"detail": "deposit observer down"}),)
        ) as service:
            with self.assertRaises(ServiceError) as raised:
                pws_client.read_funding(service.url, TOKEN)
        self.assertIn("503", str(raised.exception))
        self.assertNotIn("deposit observer down", str(raised.exception))


class FundingCommandTests(CommandTests):
    def run_funding(
        self, service_url: str, *argv: str, expires_at: int | None = None
    ) -> tuple[int, str, str]:
        return self.run_command(
            "funding", service_url, *argv, expires_at=expires_at
        )

    def test_a_shortfall_is_reported_in_whole_units(self) -> None:
        with StubService(responses=(FUNDS_NEEDED,)) as service:
            code, out, err = self.run_funding(service.url, "--required-minor-units", "4000000")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Vault address: {VAULT}", out)
        self.assertIn("Chain: ink-sepolia", out)
        self.assertIn("Card-spendable: 2.500000 USDC", out)
        self.assertIn("Withdrawable: unknown (operator required)", out)
        self.assertIn("Shortfall: 1.500000 USDC", out)
        self.assertIn("Funding: more funds needed", out)
        self.assertIn("funding instructions before transferring", out)
        self.assertNotIn(TOKEN, out)

    def test_a_ready_state_reports_that_readiness_is_not_a_reservation(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, out, err = self.run_funding(service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Funding: ready for payment", out)
        self.assertIn("not a reservation", out)
        self.assertIn("check funding with the required USDC amount", out)

    def test_card_spending_capacity_does_not_claim_withdrawal_capacity(self) -> None:
        # A fully held vault can still expose its whole balance for card spends.
        with StubService(responses=(READY | {"available": amount("14000000")},)) as service:
            code, out, err = self.run_funding(service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(service.calls, [("GET", pws_client.FUNDING_PATH)])
        self.assertIn("Card-spendable: 14.000000 USDC", out)
        self.assertEqual(
            [line for line in out.splitlines() if line.startswith("Withdrawable:")],
            ["Withdrawable: unknown (operator required)"],
        )
        self.assertNotIn("Available:", out)

    def test_a_required_amount_prices_the_readiness(self) -> None:
        with StubService(responses=(READY | {"required": amount("4000000")},)) as service:
            code, out, err = self.run_funding(
                service.url, "--required-minor-units", "4000000"
            )
            self.assertEqual(
                service.calls,
                [("GET", f"{pws_client.FUNDING_PATH}?requiredMinorUnits=4000000")],
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Next: start the purchase.", out)

    def test_an_unusable_required_amount_reports_an_action(self) -> None:
        with StubService(responses=(READY,)) as service:
            code, out, err = self.run_funding(
                service.url, "--required-minor-units", "4.00"
            )
            self.assertEqual(service.calls, [])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Required amount", err)
        self.assertIn("action: ", err)

    def test_a_pending_state_asks_for_the_same_vault_again(self) -> None:
        with StubService(responses=(PROCESSING,)) as service:
            code, out, err = self.run_funding(service.url)
            self.assertNotIn(("POST", pws_client.FUNDING_PATH), service.calls)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Funding: processing", out)
        self.assertIn("Check the same vault when the user asks to continue", out)

    def test_a_pending_state_reports_the_polled_outcome(self) -> None:
        with StubService(responses=(PROCESSING, READY)) as service:
            code, out, err = self.run_funding(service.url)
            self.assertEqual(len(service.calls), 2)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Funding: ready for payment", out)

    def test_a_timeout_stops_after_the_bounded_reads(self) -> None:
        with StubService(responses=(PROCESSING,)) as service:
            code, out, err = self.run_funding(service.url)
            self.assertEqual(
                service.calls,
                [("GET", pws_client.FUNDING_PATH)] * (pws_client.POLL_ATTEMPTS + 1),
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Funding: processing", out)


    def test_a_setup_needed_state_points_at_the_setup_command(self) -> None:
        with StubService(responses=(SETUP_NEEDED,)) as service:
            code, out, err = self.run_funding(service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Funding: setup needed", out)
        self.assertIn("run the setup command", out)

    def test_an_expired_session_is_refused_before_any_read(self) -> None:
        code, out, err = self.run_funding("https://pws.test", expires_at=1)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Session expired", err)


TOKEN_ADDRESS = "0xFabab97dCE620294D2B0b0e46C68964e326300Ac"
TRANSFER = (
    "Take test USDC from the Circle faucet and Ink Sepolia gas from the Ink faucet. "
    "Send the USDC from your own wallet to the answered vault address on Ink Sepolia. "
    "While that address is empty the vault cannot receive a transfer yet, and the faucets "
    "are still where the test USDC and the gas come from. The balance counts as available "
    "after the deposit finalizes."
)
INSTRUCTIONS = {
    "chainId": "763373",
    "tokenAddress": TOKEN_ADDRESS,
    "tokenDecimals": 6,
    "usdcFaucetUrl": "https://faucet.circle.com",
    "gasFaucetUrl": "https://inkonchain.com/faucet",
    "transferInstructions": TRANSFER,
}


def response(state: str, **fields: object) -> dict:
    return {"state": f"PARTICIPANT_FUNDING_STATE_{state}", "chain": "ink-sepolia"} | fields


CONTRACT_READY = response(
    "READY", **VAULT_FIELDS, available=amount("4000000"), instructions=INSTRUCTIONS
)
CONTRACT_FUNDS_NEEDED = response(
    "FUNDS_NEEDED", **VAULT_FIELDS, available=amount("2500000"),
    required=amount("4000000"), shortfall=amount("1500000"),
    instructions=INSTRUCTIONS,
)
EMPTY = response("FUNDS_NEEDED", **VAULT_FIELDS, available=amount("0"), instructions=INSTRUCTIONS)
CONTRACT_PROCESSING = response("PROCESSING")
BLOCKED = response("ERROR")
CONTRACT_SETUP_NEEDED = response("SETUP_NEEDED")


class FundingContractParseTests(unittest.TestCase):
    def test_actual_instruction_paragraph_and_proto_uint64_are_read(self):
        funding = pws_client.parse_funding(CONTRACT_FUNDS_NEEDED)
        self.assertEqual(str(funding.available), "2.500000 USDC")
        self.assertEqual(str(funding.required), "4.000000 USDC")
        self.assertEqual(str(funding.shortfall), "1.500000 USDC")
        self.assertEqual(funding.instructions.chain_id, 763373)
        self.assertEqual(funding.instructions.transfer_instructions, TRANSFER)
        self.assertGreater(len(TRANSFER), 200)

    def test_zero_without_a_target_has_no_shortfall(self) -> None:
        for extra in (
            {}, {"required": amount("0")},
            {"available": {"currency": "USDC", "decimals": 6}},
        ):
            with self.subTest(extra=extra):
                funding = pws_client.parse_funding(EMPTY | extra)
                self.assertEqual(funding.available.minor_units, 0)
                self.assertIsNone(funding.shortfall)

    def test_unread_balance_remains_unknown(self) -> None:
        for body in (BLOCKED, CONTRACT_PROCESSING, CONTRACT_SETUP_NEEDED):
            with self.subTest(body=body):
                funding = pws_client.parse_funding(body)
                self.assertIsNone(funding.available)
                self.assertIsNone(funding.vault_address)

    def test_balances_without_a_recorded_destination_are_supported(self) -> None:
        for body in (CONTRACT_READY, EMPTY, CONTRACT_FUNDS_NEEDED):
            body = {key: value for key, value in body.items() if key != "vaultAddress"}
            self.assertIsNone(pws_client.parse_funding(body).vault_address)

    def test_ready_zero_shortfall_is_normalized(self) -> None:
        body = CONTRACT_READY | {"shortfall": amount("0")}
        self.assertIsNone(pws_client.parse_funding(body).shortfall)

    def test_invalid_states_and_amounts_are_rejected(self) -> None:
        cases = [
            [], {}, CONTRACT_READY | {"state": "ready"},
            CONTRACT_READY | {"vaultAddress": "custody-wallet-id"},
            CONTRACT_READY | {"chain": "ink\x1b[2J"},
            CONTRACT_READY | {"available": {}},
            CONTRACT_READY | {"available": amount("-1")},
            CONTRACT_READY | {"available": amount("4.5")},
            CONTRACT_READY | {"available": amount("4", currency="USD", decimals=2)},
            CONTRACT_READY | {"available": amount("4000000", decimals=5)},
            CONTRACT_READY | {"shortfall": amount("1")},
            CONTRACT_READY | {"required": amount("5000000")},
            CONTRACT_FUNDS_NEEDED | {"shortfall": amount("2")},
            CONTRACT_FUNDS_NEEDED | {"shortfall": amount("0")},
            CONTRACT_FUNDS_NEEDED | {"available": {}},
            EMPTY | {"available": amount("1")},
        ]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ServiceError):
                pws_client.parse_funding(body)

    def test_unread_states_reject_balance_evidence(self) -> None:
        for body in (BLOCKED, CONTRACT_PROCESSING, CONTRACT_SETUP_NEEDED):
            for extra in (
                {"available": amount("0")}, {"shortfall": amount("1")},
            ):
                with self.subTest(body=body, extra=extra), self.assertRaises(ServiceError):
                    pws_client.parse_funding(body | extra)

    def test_invalid_instructions_are_rejected(self) -> None:
        for changes in (
            {"chainId": "0"}, {"chainId": True}, {"chainId": str(2**64)},
            {"tokenAddress": "not-an-address"}, {"tokenDecimals": 5},
            {"gasFaucetUrl": "javascript:alert(1)"},
            {"gasFaucetUrl": "https://[broken"},
            {"usdcFaucetUrl": "https://user:pass@example.test"},
            {"transferInstructions": "transfer\x1b[2J"},
            {"transferInstructions": "x" * 2001},
        ):
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                pws_client.parse_funding(CONTRACT_READY | {"instructions": INSTRUCTIONS | changes})

    def test_required_amount_uses_backend_u64_limit(self):
        self.assertEqual(pws_client.normalize_required_minor_units(" 0001500000 "), "1500000")
        self.assertEqual(pws_client.normalize_required_minor_units(str(2**64 - 1)), str(2**64 - 1))
        for value in ("", "-1", "1.5", str(2**64), "\u0661"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                pws_client.normalize_required_minor_units(value)


class FundingContractServiceTests(NoSleep):
    def test_get_preserves_target_and_auth_without_body(self) -> None:
        with StubService(responses=(CONTRACT_FUNDS_NEEDED,)) as service:
            funding = pws_client.read_funding(service.url, TOKEN, required_minor_units="4000000")
            self.assertEqual(service.calls, [
                ("GET", f"{pws_client.FUNDING_PATH}?requiredMinorUnits=4000000")
            ])
            self.assertEqual(service.bodies, [b""])
            self.assertEqual(service.authorizations, [f"Bearer {TOKEN}"])
        self.assertEqual(funding.state, "funds_needed")

    def test_target_must_be_echoed_exactly(self) -> None:
        for body, requested in (
            (CONTRACT_READY, "4000000"), (CONTRACT_FUNDS_NEEDED, None),
            (CONTRACT_FUNDS_NEEDED, "5000000"),
        ):
            with (
                self.subTest(body=body, requested=requested),
                StubService(responses=(body,)) as service,
            ):
                with self.assertRaises(ServiceError):
                    pws_client.read_funding(service.url, TOKEN, required_minor_units=requested)

    def test_processing_poll_is_bounded_and_preserves_target(self) -> None:
        pending = CONTRACT_PROCESSING | {"required": amount("4000000")}
        with StubService(responses=(pending,)) as service:
            funding = pws_client.poll_funding(
                service.url, TOKEN, pws_client.parse_funding(pending),
                required_minor_units="4000000", attempts=4,
            )
            self.assertEqual(service.calls, [
                ("GET", f"{pws_client.FUNDING_PATH}?requiredMinorUnits=4000000")
            ] * 4)
        self.assertEqual(funding.state, "processing")
        self.assertEqual(
            self.sleep.call_args_list,
            [unittest.mock.call(pws_client.POLL_INTERVAL_SECONDS)] * 4,
        )

    def test_funds_needed_and_blocked_do_not_automatically_poll(self) -> None:
        for body in (EMPTY, BLOCKED):
            with StubService(responses=(body,)) as service:
                pws_client.poll_funding(service.url, TOKEN, pws_client.parse_funding(body))
                self.assertEqual(service.calls, [])
        self.sleep.assert_not_called()

    def test_instruction_token_reflection_is_redacted(self) -> None:
        body = EMPTY | {"instructions": INSTRUCTIONS | {"transferInstructions": f"Send {TOKEN}"}}
        with StubService(responses=(body,)) as service:
            funding = pws_client.read_funding(service.url, TOKEN)
        self.assertNotIn(TOKEN, funding.instructions.transfer_instructions)
        self.assertIn("[redacted]", funding.instructions.transfer_instructions)

    def test_token_reflection_in_destination_is_rejected(self) -> None:
        with StubService(responses=(EMPTY,)) as service:
            with self.assertRaises(ServiceError):
                pws_client.read_funding(service.url, "ab" * 8)


class FundingContractCommandTests(CommandTests):
    def test_initializing_vault_reports_instructions_without_balance_or_top_up(self) -> None:
        # !30 and !33 attach destination metadata before issuer setup completes.
        pending = CONTRACT_PROCESSING | VAULT_FIELDS | {"instructions": INSTRUCTIONS}
        with StubService(responses=(pending,)) as service:
            code, out, err = self.run_command("funding", service.url)
            self.assertEqual(
                service.calls,
                [("GET", pws_client.FUNDING_PATH)] * (pws_client.POLL_ATTEMPTS + 1),
            )
        self.assertEqual((code, err), (0, ""))
        for text in ("Funding: processing", VAULT, TOKEN_ADDRESS, "Chain ID: 763373"):
            self.assertIn(text, out)
        for text in ("Card-spendable:", "Shortfall:", "Transfer instructions:", "more funds needed"):
            self.assertNotIn(text, out)
        self.assertIn("Withdrawable: unknown (operator required)", out)
        self.assertIn("do not request another transfer", out)

    def test_instructions_display_with_destination(self) -> None:
        with StubService(responses=(CONTRACT_FUNDS_NEEDED,)) as service:
            code, out, err = self.run_command(
                "funding", service.url, "--required-minor-units", "4000000"
            )
        self.assertEqual((code, err), (0, ""))
        for text in (
            VAULT, TOKEN_ADDRESS, "Chain ID: 763373", "Token decimals: 6",
            TRANSFER, "4.000000 USDC", "1.500000 USDC", "No token approval",
            "Do not send a second transfer",
        ):
            self.assertIn(text, out)

    def test_missing_destination_suppresses_transfer_text(self) -> None:
        body = {key: value for key, value in EMPTY.items() if key != "vaultAddress"}
        with StubService(responses=(body,)) as service:
            code, out, err = self.run_command("funding", service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Transfer instructions:", out)
        self.assertIn("missing vault destination", out)
        self.assertIn("Card-spendable: 0.000000 USDC", out)
        self.assertIn("Withdrawable: unknown (operator required)", out)
        self.assertNotIn("Withdrawable: 0", out)

    def test_blocked_is_actionable_without_a_fabricated_zero(self) -> None:
        with StubService(responses=(BLOCKED,)) as service:
            code, out, err = self.run_command("funding", service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("operator action required", out)
        self.assertNotIn("Card-spendable:", out)
        self.assertIn("Withdrawable: unknown (operator required)", out)
        self.assertIn("Do not transfer more funds", out)

    def test_readiness_without_positive_target_does_not_confirm_purchase(self) -> None:
        for target in (None, "0"):
            body = CONTRACT_READY if target is None else CONTRACT_READY | {"required": amount("0")}
            with StubService(responses=(body,)) as service:
                argv = () if target is None else ("--required-minor-units", target)
                code, out, err = self.run_command("funding", service.url, *argv)
            self.assertEqual((code, err), (0, ""))
            self.assertIn("required USDC amount", out)
            self.assertNotIn("Next: start the purchase.", out)

    def test_explicit_readiness_does_not_reserve(self) -> None:
        with StubService(responses=(CONTRACT_READY | {"required": amount("4000000")},)) as service:
            code, out, err = self.run_command(
                "funding", service.url, "--required-minor-units", "4000000"
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("not a reservation", out)
        self.assertIn("Next: start the purchase.", out)

    def test_expired_session_stops_before_reading(self) -> None:
        code, out, err = self.run_command("funding", "https://pws.test", expires_at=1)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Session expired", err)


if __name__ == "__main__":
    unittest.main()
