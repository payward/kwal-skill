import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import support  # noqa: F401

# Agent wallet mode is optional, so its tests run only where web3 is present:
# uv run --with web3==7.13.0 python -m unittest discover skills/agent-payment/tests
try:
    import wallet
    from eth_abi import encode
    from web3 import Web3
    from web3.exceptions import TransactionNotFound
except ImportError:
    wallet = None

VAULT = "0x" + "ab" * 20
AMOUNT = 20_000_000
DEPOSIT = 10**16


@unittest.skipUnless(wallet, "agent wallet mode needs web3")
class WalletTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "wallet"
        self.root.mkdir(mode=0o700)

    def test_create_reuses_saved_key_and_recovers_address(self):
        first = wallet.create_wallet(self.root)
        key_path = self.root / wallet.KEY_FILE
        key = key_path.read_bytes()
        second = wallet.create_wallet(self.root)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["address"], second["address"])
        self.assertEqual(key, key_path.read_bytes())
        self.assertTrue(second["recovery_verified"])
        self.assertNotIn(key.decode().strip(), json.dumps(second))
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)

    def test_incomplete_wallet_is_preserved(self):
        wallet.write_new(self.root / wallet.KEY_FILE, "incomplete\n")
        with self.assertRaises(ValueError):
            wallet.create_wallet(self.root)
        self.assertEqual((self.root / wallet.KEY_FILE).read_text(), "incomplete\n")

    def test_symlink_and_public_keys_are_rejected(self):
        wallet.create_wallet(self.root)
        key = self.root / wallet.KEY_FILE
        key.chmod(0o644)
        with self.assertRaises(ValueError):
            wallet.load_wallet(self.root)
        key.chmod(0o600)
        moved = self.root / "moved-key.txt"
        key.rename(moved)
        key.symlink_to(moved)
        with self.assertRaises(ValueError):
            wallet.load_wallet(self.root)

    def test_address_mismatch_is_rejected(self):
        wallet.create_wallet(self.root)
        wallet.save_json(self.root / "wallet.json", {"address": "different"})
        with self.assertRaises(ValueError):
            wallet.load_wallet(self.root)

    def test_lock_blocks_parallel_wallet_operations(self):
        with wallet.locked(self.root):
            with self.assertRaises(BlockingIOError):
                with wallet.locked(self.root):
                    self.fail("A second process must not enter the store.")

    def test_root_must_be_absolute_and_outside_a_checkout(self):
        checkout = self.root.parent / "checkout"
        (checkout / ".git").mkdir(parents=True)
        for config in ("relative", str(checkout)):
            with self.subTest(config=config), patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                    self.assertRaises(ValueError):
                wallet.wallet_root()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root.parent)}):
            self.assertEqual(wallet.wallet_root(), self.root.parent.resolve() / "pws/agent-payment/wallet")

    def test_invalid_request_ids_and_amounts_are_rejected(self):
        for request_id in ("", "Upper", "1-start", "a/b", "a" * 49):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                wallet.checked_id(request_id)
        for amount in ("NaN", "Infinity", "-1", "0", "101", "0.0000001", "bad"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                wallet.units(amount, 6, Decimal("100"))
        self.assertEqual(wallet.units("20", 6, Decimal("100")), AMOUNT)

    def test_mainnet_and_plain_http_rpcs_are_rejected(self):
        with patch.object(wallet, "Web3") as factory:
            factory.return_value.eth.chain_id = 1
            with self.assertRaisesRegex(ValueError, "chain ID"):
                wallet.connect("ink")
            factory.return_value.eth.contract.assert_not_called()
        with patch.dict(os.environ, {"TESTNET_INK_RPC": "http://example.test"}), \
                self.assertRaisesRegex(ValueError, "HTTPS"):
            wallet.connect("ink")

    def test_receipt_needs_depth_and_revert_stops_retry(self):
        web3 = MagicMock()
        entries = [{"status": "prepared", "hash": "0xabc"}]
        path = self.root / "journal.json"
        web3.eth.get_transaction_receipt.return_value = {"status": 0, "blockNumber": 10, "blockHash": b"block"}
        web3.eth.block_number = 10
        self.assertEqual(wallet.reconcile(web3, entries, path)["state"], "pending")
        web3.eth.block_number = 11
        web3.eth.get_block.return_value = {"hash": b"block"}
        with self.assertRaisesRegex(ValueError, "reverted"):
            wallet.reconcile(web3, entries, path)
        self.assertEqual(wallet.read_journal(path)[0]["status"], "reverted")
        web3.eth.send_raw_transaction.assert_not_called()

    def ink(self, usdc_units=AMOUNT):
        web3, token = MagicMock(), MagicMock()
        token.functions.transfer.return_value.call.return_value = True
        token.encode_abi.return_value = "0x"
        web3.eth.get_transaction_count.return_value = 0
        web3.eth.get_block.return_value = {"baseFeePerGas": 1}
        web3.eth.max_priority_fee = 1
        web3.eth.estimate_gas.return_value = 21000
        web3.eth.get_transaction_receipt.side_effect = TransactionNotFound("Transaction not found")
        balances = {"block": 1, "eth_wei": 10**18, "usdc_units": usdc_units}
        return web3, patch.object(wallet, "connect", return_value=(web3, token)), \
            patch.object(wallet, "read_balances", return_value=balances)

    def test_transfer_saves_before_broadcast_and_reuses_transaction(self):
        wallet.create_wallet(self.root)
        web3, connect, balances = self.ink()
        path = wallet.journal_path(self.root, "ink")
        seen = []

        def send(raw):
            self.assertEqual(wallet.read_journal(path)[-1]["raw"], Web3.to_hex(raw))
            seen.append(raw)
            raise TimeoutError("A node may accept a transaction before the timeout.")

        web3.eth.send_raw_transaction.side_effect = send
        with connect, balances:
            first = wallet.transfer_usdc(self.root, "fund-1", VAULT, AMOUNT)
            second = wallet.transfer_usdc(self.root, "fund-1", VAULT, AMOUNT)
            with self.assertRaisesRegex(ValueError, "different parameters"):
                wallet.transfer_usdc(self.root, "fund-1", VAULT, 2 * AMOUNT)
        self.assertEqual(first["state"], "unresolved")
        self.assertEqual(second["state"], "unresolved")
        self.assertEqual(len(wallet.read_journal(path)), 1)
        self.assertEqual(seen[0], seen[1])
        web3.eth.estimate_gas.assert_called_once()

    def test_confirmed_transfer_does_not_send_again(self):
        wallet.create_wallet(self.root)
        wallet.save_json(wallet.journal_path(self.root, "ink"), [{
            "hash": "0xabc", "asset": "USDC", "units": AMOUNT,
            "recipient": Web3.to_checksum_address(VAULT), "status": "confirmed", "request_id": "fund-1"}])
        web3, connect, balances = self.ink()
        with connect, balances:
            result = wallet.transfer_usdc(self.root, "fund-1", VAULT, AMOUNT)
        self.assertEqual(result["state"], "confirmed")
        web3.eth.send_raw_transaction.assert_not_called()

    def test_short_usdc_balance_asks_for_funds(self):
        address = wallet.create_wallet(self.root)["address"]
        web3, connect, balances = self.ink(usdc_units=AMOUNT - 1)
        with connect, balances:
            result = wallet.transfer_usdc(self.root, "fund-1", VAULT, AMOUNT)
        self.assertEqual((result["state"], result["address"]), ("funds_needed", address))
        web3.eth.send_raw_transaction.assert_not_called()

    def test_transfer_rejects_bad_vault_addresses(self):
        address = wallet.create_wallet(self.root)["address"]
        for recipient in ("not-an-address", address):
            with self.subTest(recipient=recipient), self.assertRaises(ValueError):
                wallet.transfer_usdc(self.root, "fund-1", recipient, AMOUNT)

    def deposit_receipt(self, address, amount):
        opaque = amount.to_bytes(32) * 2 + wallet.DEPOSIT_GAS.to_bytes(8) + b"\x00"
        topic = bytes.fromhex(address[2:]).rjust(32, b"\x00")
        return {"status": 1, "blockNumber": 10, "blockHash": bytes(32), "logs": [{
            "address": wallet.INK_PORTAL, "logIndex": 3,
            "topics": [wallet.DEPOSIT_EVENT, topic, topic, bytes(32)],
            "data": encode(["bytes"], [opaque]),
        }]}

    def test_bridge_retries_wait_for_same_deposit_without_sending_again(self):
        address = wallet.create_wallet(self.root)["address"]
        receipt = self.deposit_receipt(address, DEPOSIT)
        wallet.save_json(wallet.journal_path(self.root, "ethereum"), [{
            "hash": "0xabc", "asset": wallet.BRIDGE_ASSET, "units": DEPOSIT,
            "recipient": address, "status": "confirmed", "request_id": "bootstrap"}])
        l1, l2 = MagicMock(), MagicMock()
        l1.eth.block_number = 11
        l1.eth.get_block.return_value = {"hash": bytes(32)}
        l2.eth.get_transaction_receipt.side_effect = TransactionNotFound("Not yet derived")
        with patch.object(wallet, "connect", side_effect=lambda name: (l1 if name == "ethereum" else l2, None)), \
                patch.object(wallet, "submit") as submit:
            l1.eth.get_transaction_receipt.side_effect = TransactionNotFound("RPC backend lag")
            self.assertEqual(wallet.bridge_eth(self.root, "bootstrap", DEPOSIT)["state"], "pending")
            l1.eth.get_transaction_receipt.side_effect = None
            l1.eth.get_transaction_receipt.return_value = receipt
            first = wallet.bridge_eth(self.root, "bootstrap", DEPOSIT)
            self.assertEqual(first["state"], "bridge_pending")
            self.assertEqual(first, wallet.bridge_eth(self.root, "bootstrap", DEPOSIT))
            l2.eth.get_transaction_receipt.side_effect = None
            l2.eth.get_transaction_receipt.return_value = {"status": 1, "blockNumber": 20, "blockHash": b"l2"}
            l2.eth.block_number = 21
            l2.eth.get_block.return_value = {"hash": b"l2"}
            self.assertEqual(wallet.bridge_eth(self.root, "bootstrap", DEPOSIT)["state"], "bridged")
            with self.assertRaisesRegex(ValueError, "different parameters"):
                wallet.bridge_eth(self.root, "bootstrap", 2 * DEPOSIT)
            submit.assert_not_called()
        with self.assertRaisesRegex(ValueError, "does not match"):
            wallet.deposit_hash(receipt, address, 2 * DEPOSIT)
        with self.assertRaisesRegex(ValueError, "one Ink deposit"):
            wallet.deposit_hash({**receipt, "logs": []}, address, DEPOSIT)

    def test_new_bridge_deposit_calls_the_ink_portal(self):
        address = wallet.create_wallet(self.root)["address"]
        l1 = MagicMock()
        l1.eth.contract.return_value = Web3().eth.contract(abi=wallet.PORTAL_ABI)
        with patch.object(wallet, "connect", return_value=(l1, None)), \
                patch.object(wallet, "submit", return_value={"state": "pending"}) as submit:
            wallet.bridge_eth(self.root, "new", DEPOSIT)
        tx, entry = submit.call_args.args[3], submit.call_args.args[4]
        self.assertEqual((tx["to"], tx["value"]), (wallet.INK_PORTAL, DEPOSIT))
        call, arguments = l1.eth.contract.return_value.decode_function_input(tx["data"])
        self.assertEqual(call.fn_name, "depositTransaction")
        self.assertEqual(arguments["to"], address)
        self.assertEqual(entry["asset"], wallet.BRIDGE_ASSET)


if __name__ == "__main__":
    unittest.main()
