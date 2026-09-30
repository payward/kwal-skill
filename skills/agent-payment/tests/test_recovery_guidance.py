"""Recovery directions match the observed quote and funding state."""

import unittest

from support import CommandTests, StubService, amount

import pws_client  # noqa: E402

QUOTE_ID = "quote_recovery"
QUOTE_PATH = f"{pws_client.QUOTES_PATH}/{QUOTE_ID}"
EXPIRED_QUOTE = {
    "quoteId": QUOTE_ID,
    "lines": [{"variantId": "variant_recovery", "quantity": 1}],
    "shippingOptions": [
        {
            "shippingOptionId": "ship_standard",
            "label": "Standard",
            "price": amount("699", currency="USD", decimals=2),
        },
        {
            "shippingOptionId": "ship_fast",
            "label": "Two days",
            "price": amount("2199", currency="USD", decimals=2),
        },
    ],
    "selectedShippingOptionId": "ship_standard",
    "total": amount("919", currency="USD", decimals=2),
    "expiresAtUnixSeconds": "1",
}

VAULT_ADDRESS = f"0x{'ab' * 20}"
TOKEN_ADDRESS = f"0x{'cd' * 20}"
TRANSFER_INSTRUCTIONS = "Send test USDC from your preferred wallet to this vault."
INSTRUCTIONS = {
    "chainId": "763373",
    "tokenAddress": TOKEN_ADDRESS,
    "tokenDecimals": 6,
    "usdcFaucetUrl": "https://faucet.circle.com",
    "gasFaucetUrl": "https://inkonchain.com/faucet",
    "transferInstructions": TRANSFER_INSTRUCTIONS,
}
FUNDING_DESTINATION = {
    "vaultAddress": VAULT_ADDRESS,
    "chain": "eip155:763373",
    "instructions": INSTRUCTIONS,
}


class ShippingRecoveryTests(CommandTests):
    def test_expired_quote_with_different_shipping_stops_before_any_write(self):
        with StubService(payload=EXPIRED_QUOTE) as service:
            code, out, err = self.run_command(
                "shipping", service.url, QUOTE_ID, "--option", "ship_fast"
            )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(service.calls, [("GET", QUOTE_PATH)])
        self.assertIn("Quote: expired", out)
        self.assertIn("prepare a fresh quote", out)
        self.assertIn("same variant, quantity, delivery address, and shipping preference", out)
        self.assertIn("obtain purchase authorization", out)
        self.assertIn("Do not reuse this quote id", out)

    def test_attached_payment_recovery_takes_precedence_over_quote_expiry(self):
        reserved = EXPIRED_QUOTE | {"paymentId": "payment_recovery"}
        for option in ("ship_standard", "ship_fast"):
            with self.subTest(option=option):
                with StubService(payload=reserved) as service:
                    code, out, err = self.run_command(
                        "shipping", service.url, QUOTE_ID, "--option", option
                    )
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(service.calls, [("GET", QUOTE_PATH)])
                self.assertIn("Attached payment: payment_recovery", out)
                self.assertIn("read payment payment_recovery with the payment command", out)
                self.assertIn("Do not submit another checkout", out)
                self.assertNotIn("prepare a fresh quote", out)
                self.assertNotIn("Quote: expired\n", out)


class FundingRecoveryTests(CommandTests):
    def assert_destination_metadata(self, output):
        self.assertIn(f"Vault address: {VAULT_ADDRESS}", output)
        self.assertIn("Chain: eip155:763373", output)
        self.assertIn("Chain ID: 763373", output)
        self.assertIn(f"Token address: {TOKEN_ADDRESS}", output)
        self.assertIn("Token decimals: 6", output)

    def assert_no_deposit_prompt(self, output):
        self.assertNotIn("Transfer instructions:", output)
        self.assertNotIn(TRANSFER_INSTRUCTIONS, output)
        self.assertNotIn("USDC faucet:", output)
        self.assertNotIn("Gas faucet:", output)
        self.assertNotIn(INSTRUCTIONS["usdcFaucetUrl"], output)
        self.assertNotIn(INSTRUCTIONS["gasFaucetUrl"], output)
        self.assertNotIn("Next: transfer USDC", output)

    def test_ready_funding_retains_metadata_without_another_deposit_prompt(self):
        for required in (None, "5000000"):
            with self.subTest(required=required):
                body = FUNDING_DESTINATION | {
                    "state": "PARTICIPANT_FUNDING_STATE_READY",
                    "available": amount("5000000"),
                }
                args = ()
                path = pws_client.FUNDING_PATH
                if required is not None:
                    body |= {"required": amount(required)}
                    args = ("--required-minor-units", required)
                    path += f"?requiredMinorUnits={required}"
                with StubService(payload=body) as service:
                    code, out, err = self.run_command("funding", service.url, *args)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(service.calls, [("GET", path)])
                self.assert_destination_metadata(out)
                self.assert_no_deposit_prompt(out)
                self.assertIn("Card-spendable: 5.000000 USDC", out)
                self.assertIn("Withdrawable: unknown (operator required)", out)
                self.assertIn("Funding: ready for payment", out)
                self.assertIn("Readiness is not a reservation", out)
                if required is None:
                    self.assertIn("check funding with the required USDC amount", out)
                else:
                    self.assertIn("Required: 5.000000 USDC", out)
                    self.assertIn("Next: start the purchase", out)

    def test_processing_retains_metadata_without_prompting_a_second_deposit(self):
        body = FUNDING_DESTINATION | {
            "state": "PARTICIPANT_FUNDING_STATE_PROCESSING",
        }
        with StubService(payload=body) as service:
            code, out, err = self.run_command("funding", service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            service.calls,
            [("GET", pws_client.FUNDING_PATH)] * (pws_client.POLL_ATTEMPTS + 1),
        )
        self.assert_destination_metadata(out)
        self.assert_no_deposit_prompt(out)
        self.assertIn("Funding: processing", out)
        self.assertIn("these destination details do not request another transfer", out)
        self.assertIn("Check the same vault when the user asks to continue", out)

    def test_funds_needed_keeps_transfer_details_and_duplicate_deposit_warning(self):
        body = FUNDING_DESTINATION | {
            "state": "PARTICIPANT_FUNDING_STATE_FUNDS_NEEDED",
            "available": amount("5000000"),
            "required": amount("9190000"),
            "shortfall": amount("4190000"),
        }
        with StubService(payload=body) as service:
            code, out, err = self.run_command(
                "funding", service.url, "--required-minor-units", "9190000"
            )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            service.calls,
            [("GET", f"{pws_client.FUNDING_PATH}?requiredMinorUnits=9190000")],
        )
        self.assert_destination_metadata(out)
        self.assertIn("Shortfall: 4.190000 USDC", out)
        self.assertIn("Funding: more funds needed", out)
        self.assertIn(f"Transfer instructions: {TRANSFER_INSTRUCTIONS}", out)
        self.assertIn(f"USDC faucet: {INSTRUCTIONS['usdcFaucetUrl']}", out)
        self.assertIn(f"Gas faucet: {INSTRUCTIONS['gasFaucetUrl']}", out)
        self.assertIn("Next: transfer USDC to the printed vault", out)
        self.assertIn("Do not send a second transfer solely because this state persists", out)


if __name__ == "__main__":
    unittest.main()
