"""Payment recovery stays isolated and survives concurrent callers."""

from argparse import Namespace
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import tempfile
import unittest
from unittest.mock import patch

from support import CommandTests
import pws_client
import checkout
from test_checkout import QUOTE, REQUIRES_ACTION


def record_in_process(args):
    path, index = args
    store = pws_client.AttemptStore(Path(path))
    return store.record(pws_client.Attempt(f"pay_{index}", f"quote_{index % 5}"))


class PaymentRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def store(self, name="alice.json"):
        return pws_client.AttemptStore(self.root / name)

    def test_credentials_in_one_directory_have_separate_histories(self):
        alice, bob = self.store(), self.store("bob.json")
        first = pws_client.Attempt("pay_alice", "quote_1")
        second = pws_client.Attempt("pay_bob", "quote_1")
        alice.record(first)
        self.assertIsNone(bob.latest())
        bob.record(second)
        self.assertEqual(alice.latest(), first)
        self.assertEqual(bob.latest(), second)

    def concurrent_records(self, same_quote):
        barrier = Barrier(12)

        def record(index):
            store = self.store()
            attempt = pws_client.Attempt(
                f"pay_{index}", "quote_1" if same_quote else f"quote_{index}"
            )
            barrier.wait(timeout=10)
            return store.record(attempt)

        with ThreadPoolExecutor(max_workers=12) as pool:
            return list(pool.map(record, range(12)))

    def test_concurrent_callers_reuse_the_first_id_for_one_quote(self):
        returned = self.concurrent_records(same_quote=True)
        saved = self.store().load()
        self.assertEqual(len(saved), 1)
        self.assertEqual(returned, [saved[0]] * 12)

    def test_concurrent_different_quotes_keep_every_attempt(self):
        returned = self.concurrent_records(same_quote=False)
        saved = self.store().load()
        self.assertEqual(len(saved), 12)
        self.assertEqual(set(saved), set(returned))
        self.assertEqual(self.store().path.stat().st_mode & 0o777, 0o600)

    def test_separate_processes_preserve_each_quote_and_its_original_id(self):
        with ProcessPoolExecutor(max_workers=4) as pool:
            returned = list(pool.map(
                record_in_process,
                [(str(self.root / "alice.json"), index) for index in range(20)],
            ))
        saved = self.store().load()
        self.assertEqual(len(saved), 5)
        self.assertEqual(set(returned), set(saved))
        for attempt in returned:
            self.assertEqual(attempt, self.store().find(attempt.quote_id))


class ConcurrentCheckoutTests(CommandTests):
    def test_racing_checkouts_submit_the_same_saved_id(self):
        barrier = Barrier(2)
        sent = []
        credentials = pws_client.Credentials(
            "https://example.test", "test-token", 9999999999
        )
        args = Namespace(credentials=str(self.path), quote_id="quote_1")

        def read(*_):
            barrier.wait(timeout=10)
            return pws_client.parse_quote(QUOTE)

        def submit(_url, _token, payment_id, _quote_id):
            sent.append(payment_id)
            return pws_client.parse_payment(REQUIRES_ACTION | {"paymentId": payment_id})

        with (
            patch.object(checkout, "_session", return_value=credentials),
            patch.object(checkout, "read_quote", side_effect=read),
            patch.object(checkout, "create_checkout", side_effect=submit),
            patch.object(checkout, "_print_quote"),
            patch.object(checkout, "_review_payment", return_value=0),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            list(pool.map(lambda _: checkout._command_checkout(args, 0), range(2)))

        saved = pws_client.AttemptStore(self.path).load()
        self.assertEqual(len(saved), 1)
        self.assertEqual(sent, [saved[0].payment_id] * 2)
