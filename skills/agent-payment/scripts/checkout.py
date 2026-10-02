"""Checkout submission, saved attempts, and payment status."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from _fields import (
    _field_instant,
    _field_state,
    _field_text,
    _object,
    _route_path,
    normalize_identifier,
)
from amounts import Amount, _amount
from cli_support import _format_expiry, unknown_checkout_outcome
from errors import ConfigurationError, ServiceError
from quotes import _print_quote, _quote_refusal, read_quote
from session import _OWNER_ONLY, _session, resolve_credentials_path
from transport import POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, _participant_json

PAYMENTS_PATH = "/kwal/participant/v1/payments"


PAYMENT_STATES = frozenset(
    {"requires_action", "pending", "completed", "declined", "error"}
)

# The steps of a sandbox payment made by a simulated card spend.
SIMULATED_STEPS = frozenset({"card_authorization", "card_clearing"})

# The service names a quote another payment already holds with this step, so
# the agent can tell it from every other stop.
QUOTE_ALREADY_USED_STEP = "quote_already_used"
_QUOTE_ALREADY_PAID = (
    "Next: this quote already belongs to another payment. Reap returns the same"
    " quote for the same item and quantity for about 15 minutes, so buying the"
    " same basket again inside that window reaches the paid quote. Choose"
    " another item or quantity, or wait and create a new quote. Do not submit"
    " this quote again."
)

# A checkout the vault cannot cover is refused before anything is saved or
# sent, with this typed service error.
_FUNDING_REFUSAL = "service_error=ParticipantFundingRequest"

# One card carries one sandbox payment at a time. The service saves nothing for
# a busy card, and it names the holding payment only when it is the caller's.
_CARD_BUSY = re.compile(r"service_error=ParticipantCardBusy(?:; holding_payment=([^;)]+))?")


def _field_link(body: dict[str, Any], key: str, *, subject: str) -> str | None:
    """A human opens the approval link in a browser, so a value that is not an
    https URL is a contract mismatch and never a label to print."""
    link = _field_text(body, key, subject=subject, required=False)
    if link is None:
        return None
    try:
        parts = urllib.parse.urlsplit(link)
        host = parts.hostname
    except ValueError as error:
        raise ServiceError(
            f"{subject} response field {key} is not a readable link."
        ) from error
    # A userinfo prefix lets the printed label name a host that the browser
    # never opens, so the human would approve on an unexpected page.
    if parts.scheme != "https" or not host or "@" in parts.netloc:
        raise ServiceError(f"{subject} response field {key} is not an https link.")
    return link


@dataclasses.dataclass(frozen=True)
class Payment:
    """The participant API owns the checkout. This record carries what the
    agent reports and what decides the next step."""

    payment_id: str
    state: str
    step: str | None
    approval_url: str | None
    approval_expires_at: int | None
    checkout_id: str | None
    order_id: str | None
    charged: Amount | None
    reason: str | None
    amount: Amount | None
    checkout_status: str | None
    quote_id: str | None
    # A sandbox payment is a simulated swipe of the participant's own card:
    # it names the card transaction and what the vault still holds for it.
    card_transaction_id: str | None = None
    held: Amount | None = None

    def is_simulated_card_spend(self) -> bool:
        return self.step in SIMULATED_STEPS

    def approval_is_expired(self, now: int) -> bool:
        return self.approval_expires_at is not None and now >= self.approval_expires_at


def parse_payment(body: Any) -> Payment:
    """Fields can be absent because proto3 JSON omits a default and
    because the checkout reaches each reference at a different step. A state
    the agent cannot act on is refused, and a state that carries a failure
    keeps whatever the service reported about it."""
    subject = "Payment"
    payment = _object(body, subject=subject)
    state = _field_state(
        payment,
        prefix="PARTICIPANT_PAYMENT_STATE_",
        states=PAYMENT_STATES,
        subject=subject,
    )
    approval_url = _field_link(payment, "approvalUrl", subject=subject)
    approval_expires_at = _field_instant(
        payment, "approvalExpiresAtUnixSeconds", subject=subject, required=False
    )
    # The human approves on the hosted page, so a state that waits for that
    # approval cannot leave the link unreported.
    if state == "requires_action" and approval_url is None:
        raise ServiceError(f"{subject} response requires action without a link.")
    order_id = _field_text(payment, "orderId", subject=subject, required=False)
    return Payment(
        payment_id=_field_text(payment, "paymentId", subject=subject, required=True),
        state=state,
        step=_field_text(payment, "step", subject=subject, required=False),
        approval_url=approval_url,
        approval_expires_at=approval_expires_at,
        checkout_id=_field_text(payment, "checkoutId", subject=subject, required=False),
        order_id=order_id,
        charged=_amount(payment, "charged", subject=subject),
        reason=_field_text(payment, "reason", subject=subject, required=False),
        amount=_amount(payment, "amount", subject=subject),
        checkout_status=_field_text(payment, "checkoutStatus", subject=subject, required=False),
        quote_id=_field_text(payment, "quoteId", subject=subject, required=False),
        card_transaction_id=_field_text(
            payment, "cardTransactionId", subject=subject, required=False
        ),
        held=_amount(payment, "held", subject=subject),
    )


def _matching_payment(
    payment: Payment, payment_id: str, quote_id: str | None = None
) -> Payment:
    if payment.payment_id != payment_id:
        raise ServiceError("Payment response does not match the requested payment.")
    if (
        quote_id is not None
        and payment.quote_id is not None
        and payment.quote_id != quote_id
    ):
        raise ServiceError("Payment response does not match the requested quote.")
    return payment


def create_checkout(
    service_url: str, token: str, payment_id: str, quote_id: str
) -> Payment:
    payment = parse_payment(
        _participant_json(
            service_url,
            PAYMENTS_PATH,
            token,
            method="POST",
            payload={"paymentId": payment_id, "quoteId": quote_id},
            subject="Payment",
        )
    )
    return _matching_payment(payment, payment_id, quote_id)


def read_payment(
    service_url: str, token: str, payment_id: str, *, quote_id: str | None = None
) -> Payment:
    payment = parse_payment(
        _participant_json(
            service_url,
            _route_path(PAYMENTS_PATH, payment_id),
            token,
            subject="Payment",
        )
    )
    return _matching_payment(payment, payment_id, quote_id)


def poll_payment(
    service_url: str,
    token: str,
    payment: Payment,
    *,
    attempts: int = POLL_ATTEMPTS,
    quote_id: str | None = None,
) -> Payment:
    """Polling is bounded, and a timeout returns the current state so a caller
    never submits a second checkout for the same attempt. A payment that
    requires action waits for the human, so it is not polled."""
    payment_id = payment.payment_id
    expected_quote = quote_id if quote_id is not None else payment.quote_id
    _matching_payment(payment, payment_id, expected_quote)
    for _ in range(attempts):
        if payment.state != "pending":
            return payment
        time.sleep(POLL_INTERVAL_SECONDS)
        payment = read_payment(service_url, token, payment_id, quote_id=expected_quote)
        if expected_quote is None:
            expected_quote = payment.quote_id
    return payment


ATTEMPTS_FILE_NAME = "payments.json"


def new_payment_id() -> str:
    return f"pay_{uuid.uuid4().hex}"


def _sync_directory(directory: Path) -> None:
    """A rename is durable only after the directory entry itself reaches the
    disk, so the record survives a crash between the submission and a
    reboot."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclasses.dataclass(frozen=True)
class Attempt:
    payment_id: str
    quote_id: str


class AttemptStore:
    """The payment id must outlive the process, so it is kept beside the
    credentials, which the helper already holds outside any checkout. The
    record carries no secret, so it needs none of the token hardening."""

    def __init__(self, credentials_path: Path) -> None:
        self.path = credentials_path.with_name(
            f"{credentials_path.name}.{ATTEMPTS_FILE_NAME}"
        )

    def load(self) -> tuple[Attempt, ...]:
        try:
            document = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigurationError(
                f"Could not read the payment record at {self.path}."
            ) from error
        try:
            raw = json.loads(document)
        except json.JSONDecodeError as error:
            raise ConfigurationError(
                f"The payment record at {self.path} is not readable."
            ) from error
        if not isinstance(raw, list):
            raise ConfigurationError(
                f"The payment record at {self.path} is not readable."
            )
        return tuple(self._attempt(entry) for entry in raw)

    def _attempt(self, entry: Any) -> Attempt:
        payment_id = entry.get("payment_id") if isinstance(entry, dict) else None
        quote_id = entry.get("quote_id") if isinstance(entry, dict) else None
        if not isinstance(payment_id, str) or not isinstance(quote_id, str):
            raise ConfigurationError(
                f"The payment record at {self.path} is not readable."
            )
        return Attempt(payment_id=payment_id, quote_id=quote_id)

    def record(self, attempt: Attempt) -> Attempt:
        # Lock a stable sibling, not the file replaced below. Two callers
        # must keep every quote and use the same id for the same quote.
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, _OWNER_ONLY)
            with os.fdopen(descriptor, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                existing = self.find(attempt.quote_id)
                if existing is not None:
                    return existing
                self._save(attempt)
                return attempt
        except OSError as error:
            raise ConfigurationError(
                f"Could not lock or save the payment record at {self.path}."
            ) from error

    def _save(self, attempt: Attempt) -> None:
        # The replacement is staged, flushed to the disk, and then moved into
        # place, so neither an interrupted write nor a host crash can lose the
        # id of an attempt already sent. Every attempt is kept, because a
        # dropped record would let the same quote reach a second checkout
        # under a new payment id.
        kept = (*self.load(), attempt)
        staged = self.path.with_name(f"{self.path.name}.{os.getpid()}.new")
        document = (
            json.dumps([dataclasses.asdict(one) for one in kept], indent=2) + "\n"
        )
        try:
            with open(staged, "w", encoding="utf-8") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            staged.chmod(_OWNER_ONLY)
            os.replace(staged, self.path)
            _sync_directory(self.path.parent)
        except OSError as error:
            staged.unlink(missing_ok=True)
            raise ConfigurationError(
                f"Could not save the payment record at {self.path}: {error.strerror}"
            ) from error

    def find(self, quote_id: str) -> Attempt | None:
        for attempt in reversed(self.load()):
            if attempt.quote_id == quote_id:
                return attempt
        return None

    def latest(self) -> Attempt | None:
        attempts = self.load()
        return attempts[-1] if attempts else None


_PAYMENT_STATE_LINES = {
    "requires_action": "Payment: waiting for the human approval",
    "pending": "Payment: processing",
    "completed": "Payment: completed",
    "declined": "Payment: declined",
    "error": "Payment: stopped for operator help",
}


def _print_payment(payment: Payment, now: int) -> None:
    print(f"Payment id: {payment.payment_id}")
    print(_PAYMENT_STATE_LINES[payment.state])
    if payment.amount is not None:
        print(f"Quoted amount: {payment.amount}")
    if payment.checkout_status is not None:
        print(f"Checkout status: {payment.checkout_status}")
    if payment.quote_id is not None:
        print(f"Attached quote: {payment.quote_id}")
    if payment.step is not None:
        print(f"Step: {payment.step}")
    if payment.charged is not None:
        print(f"Charged: {payment.charged}")
    # Only a simulated card spend is decided by the card transaction; an
    # agentic checkout's order is its outcome.
    if payment.is_simulated_card_spend():
        if payment.held is not None:
            print(f"Held on the vault: {payment.held}")
        if payment.card_transaction_id is not None:
            print(f"Card transaction id: {payment.card_transaction_id}")
    # Only a waiting payment asks the human to approve, so a link reported
    # beside any other state would send the human to a page that decides
    # nothing.
    if payment.state == "requires_action" and payment.approval_url is not None:
        print(f"Approval link: {payment.approval_url}")
        if payment.approval_expires_at is None:
            print("Approval link expiry: not supplied by Reap")
        else:
            print(
                f"Approval link expires at: {payment.approval_expires_at}"
                f" ({_format_expiry(payment.approval_expires_at, now)})"
            )
    if payment.checkout_id is not None:
        print(f"Checkout id: {payment.checkout_id}")
    if payment.order_id is not None:
        print(f"Order id: {payment.order_id}")
    if payment.reason is not None:
        print(f"Reason: {payment.reason}")


def _review_payment(payment: Payment, now: int) -> int:
    """A submission and a resume reach the same attempt, so both report one
    next step and no reported state asks for a second checkout."""
    _print_payment(payment, now)
    if payment.state == "requires_action":
        if payment.approval_is_expired(now):
            print("Approval link: expired")
            print(
                "Next: read the same payment again. If the link stays expired,"
                " ask the operator to resolve this attempt. Link expiry does"
                " not determine the payment outcome. Do not submit a second checkout."
            )
            return 0
        print(
            "Next: ask the human to open the approval link, approve the"
            " payment on the hosted page, and complete any provider challenge"
            " there. The agent never receives the card number. Then run the"
            " payment command again for this payment id."
        )
        return 0
    if payment.is_simulated_card_spend():
        print(
            "Sandbox: this payment is a simulated card spend against the vault."
            " No shop receives an order and nothing ships."
        )
    if payment.state == "pending":
        print(
            "Next: report the state. When the user asks to continue, run the"
            " payment command for this payment id."
            " Do not submit a second checkout."
        )
        return 0
    if payment.state == "completed":
        if payment.is_simulated_card_spend():
            print("Settlement: the vault paid the simulated card spend")
            print("Next: report the completed sandbox payment to the user.")
            return 0
        print("Settlement: not verified by this response")
        print("Next: report order completion to the user.")
        return 0
    if payment.state == "declined":
        print(
            "Next: report the decline to the user. A new attempt needs a fresh"
            " quote the user approves. Do not submit this quote again."
        )
        return 0
    if payment.step == QUOTE_ALREADY_USED_STEP:
        print(_QUOTE_ALREADY_PAID)
        return 0
    print(
        "Next: report the step and the reason to the user. Ask the operator to"
        " resolve the recorded outcome. Do not submit a second checkout."
    )
    return 0


def _attempt_store(args: argparse.Namespace) -> AttemptStore:
    return AttemptStore(resolve_credentials_path(args.credentials))


def _command_checkout(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    quote_id = normalize_identifier(args.quote_id)
    store = _attempt_store(args)
    attempt = store.find(quote_id)
    if attempt is None:
        quote = read_quote(credentials.service_url, credentials.token, quote_id)
        _print_quote(quote, now)
        refusal = _quote_refusal(quote, now)
        if refusal is not None:
            return refusal
        # The id is recorded before the submission, so a submission that is
        # sent and never answered stays resumable under the same id.
        attempt = Attempt(payment_id=new_payment_id(), quote_id=quote_id)
        attempt = store.record(attempt)
    else:
        print("Checkout: resuming the recorded attempt")
    try:
        payment = create_checkout(
            credentials.service_url, credentials.token, attempt.payment_id, quote_id
        )
    except ServiceError as error:
        card_busy = _CARD_BUSY.search(str(error))
        if card_busy:
            _print_card_busy(card_busy.group(1))
            return 1
        if unknown_checkout_outcome(str(error)):
            print(f"Payment id: {attempt.payment_id}")
            raise
        if _FUNDING_REFUSAL not in str(error):
            raise
        print("Checkout: the vault cannot cover this quote. Nothing was sent.")
        print(
            f"Next: run quote-check {quote_id} to see the shortfall and the"
            " deposit instructions, fund the vault, then run the same checkout"
            " again. It reuses the recorded payment id."
        )
        return 1
    return _review_payment(payment, now)


def _print_card_busy(holder: str | None) -> None:
    print(
        "Checkout: another payment holds your card. Nothing was saved for this"
        " payment."
    )
    if holder is None:
        print(
            "Next: wait until the other payment on your card finishes, then run"
            " the same checkout again. It reuses the recorded payment id."
        )
        return
    print(
        f"Next: wait until payment {holder} finishes. Read it with: python3"
        f" scripts/register.py payment {holder}. When it is completed or"
        " declined, run the same checkout again. It reuses the recorded"
        " payment id."
    )


def _command_payment(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    if args.payment_id is not None:
        payment_id = normalize_identifier(args.payment_id)
        # An explicit payment ID remains readable when its local record is
        # unusable. Use a matching saved quote when the record is available.
        try:
            attempts = _attempt_store(args).load()
        except ConfigurationError:
            attempts = ()
        recorded = next(
            (
                attempt
                for attempt in reversed(attempts)
                if attempt.payment_id == payment_id
            ),
            None,
        )
    else:
        # The agent can restart between the submission and the outcome, so the
        # recorded attempt names the payment the helper observes.
        recorded = _attempt_store(args).latest()
        if recorded is None:
            print("Payment: none recorded")
            print(
                "Next: ask the user for the payment id if a prior submission"
                " may exist. Resolve that attempt before a new checkout."
            )
            return 0
        payment_id = recorded.payment_id

    quote_id = recorded.quote_id if recorded is not None else None
    payment = read_payment(
        credentials.service_url, credentials.token, payment_id, quote_id=quote_id
    )
    payment = poll_payment(
        credentials.service_url, credentials.token, payment, quote_id=quote_id
    )
    return _review_payment(payment, now)
