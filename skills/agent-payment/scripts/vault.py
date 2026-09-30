"""Vault setup, funding readiness, and their commands."""

from __future__ import annotations

import argparse
import dataclasses
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

from _fields import (
    _field_address,
    _field_enum,
    _field_count,
    _field_state,
    _field_text,
    _shaped_address,
    _whole_number,
    _without_token,
)
from amounts import Amount, _amount
from errors import ConfigurationError, ServiceError
from session import _session
from transport import POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, request_json

STATUS_PATH = "/kwal/participant/v1/status"


INITIALIZE_PATH = "/kwal/participant/v1/initialize"


SETUP_WAIT_SECONDS = 300


SETUP_STATES = frozenset({"not_started", "pending", "ready", "needs_operator"})
ENROLLMENT_STATUSES = frozenset(
    {"REQUIRES_ACTION", "ACTIVE", "FAILED", "EXPIRED", "REVOKED"}
)


def normalize_owner_address(owner_address: str) -> str:
    """The owner address stays unverified: the human keeps the key and never
    proves control, so only its shape can be checked here."""
    address = owner_address.strip()
    if not _shaped_address(address):
        raise ConfigurationError(
            "Owner address must be 0x followed by 40 non-zero hexadecimal digits."
        )
    return address


@dataclasses.dataclass(frozen=True)
class Setup:
    """The participant API owns setup progress. This record only carries what
    the agent shows and what decides the next step."""

    state: str
    step: str | None
    vault_address: str | None
    chain: str | None
    owner_address: str | None = None
    card_status: str | None = None
    enrollment_id: str | None = None
    enrollment_status: str | None = None
    deployment_tx_id: str | None = None


def parse_setup(body: Any) -> Setup:
    if not isinstance(body, dict):
        raise ServiceError("Setup response is not an object.")

    state = _field_state(body, prefix="PARTICIPANT_SETUP_STATE_", states=SETUP_STATES, subject="Setup")
    setup = Setup(
        enrollment_id=_field_text(body, "enrollmentId", subject="Setup", required=False),
        enrollment_status=_field_enum(
            body,
            "enrollmentStatus",
            prefix="PARTICIPANT_ENROLLMENT_STATUS_",
            values=ENROLLMENT_STATUSES,
            subject="Setup",
        ),
        state=state,
        step=_field_text(body, "step", subject="Setup", required=False),
        vault_address=_field_address(
            body, "vaultAddress", subject="Setup", required=False
        ),
        chain=_field_text(body, "chain", subject="Setup", required=False),
        owner_address=_field_address(
            body, "ownerAddress", subject="Setup", required=False
        ),
        card_status=_field_text(body, "cardStatus", subject="Setup", required=False),
        deployment_tx_id=_field_text(body, "deploymentTxId", subject="Setup", required=False),
    )
    if setup.deployment_tx_id is not None and not re.fullmatch(
        r"0x[0-9a-fA-F]{64}", setup.deployment_tx_id
    ):
        raise ServiceError("Setup response has an invalid deployment transaction hash.")
    if (setup.enrollment_id is None) != (setup.enrollment_status is None):
        raise ServiceError("Setup response carries incomplete enrollment evidence.")
    # A sandbox that pays with simulated card spends reports no enrollment at
    # all; any enrollment it does report must be active.
    if setup.state == "ready" and (setup.enrollment_status not in (None, "ACTIVE") or setup.card_status != "ACTIVE" or body.get("depositObserved") is not True):
        raise ServiceError("Setup response reports ready without active card and deposit evidence.")
    # Only an operator clears a non-active enrollment, so any other state would
    # poll a dead enrollment and then send the user on toward funding.
    if setup.enrollment_status not in (None, "ACTIVE") and (setup.state != "needs_operator" or setup.step != "card_enrollment"):
        raise ServiceError("Setup response reports a non-active enrollment without operator attention.")
    if setup.card_status not in (None, "ACTIVE", "UNVERIFIED"):
        raise ServiceError("Setup response carries an unknown card status.")
    # Any saved progress is the record that setup exists, so a not-started
    # state that carries some would invite a replacement vault or card.
    if setup.state == "not_started" and (
        setup.owner_address is not None or setup.vault_address is not None or setup.step is not None
        or setup.enrollment_id is not None or setup.card_status is not None
        or setup.deployment_tx_id is not None
    ):
        raise ServiceError("Setup response reports not started with setup progress.")
    # Checkout readiness requires a vault, so a ready state without one is a
    # contract mismatch, not a success.
    if setup.state == "ready" and setup.vault_address is None:
        raise ServiceError("Setup response reports ready without a vault.")
    if setup.vault_address is not None and setup.chain is None:
        raise ServiceError("Setup response reports a vault without its chain.")
    return setup


def _without_token_in_setup(setup: Setup, token: str) -> Setup:
    # A transaction hash is recovery evidence, so redacting part of it would
    # invent a different identifier. Refuse reflected credentials instead.
    if setup.deployment_tx_id is not None and token in setup.deployment_tx_id:
        raise ServiceError("Setup response reports the session token in a deployment transaction.")
    return _without_token(
        setup,
        token,
        subject="Setup",
        addresses=("owner_address", "vault_address"),
        texts=("chain", "step", "card_status", "enrollment_id", "enrollment_status"),
    )


def read_setup(service_url: str, token: str, *, deadline: float | None = None) -> Setup:
    # KWeb response_body: "setup" projects the field into the HTTP body.
    body = request_json(service_url, STATUS_PATH, token=token, deadline=deadline)
    return _without_token_in_setup(parse_setup(body), token)


def initialize_setup(
    service_url: str, token: str, owner_address: str, *, deadline: float | None = None,
) -> Setup:
    body = request_json(
        service_url,
        INITIALIZE_PATH,
        method="POST",
        payload={"ownerAddress": owner_address},
        token=token,
        deadline=deadline,
    )
    setup = _without_token_in_setup(parse_setup(body), token)
    if setup.state == "not_started":
        raise ServiceError("Setup response lost the initialized setup.")
    return setup


def _can_resume_setup(setup: Setup) -> bool:
    # Only server-confirmed pending steps may resume using the saved owner.
    # Uncertain resource writes and explicit operator stops are never retried.
    return (
        setup.state == "pending"
        and setup.owner_address is not None
        and (
            setup.step in {
                "issuer_setup", "sandbox_approval", "account_creation",
                "account_verification", "card_verification",
            }
            or (setup.step == "card_enrollment" and setup.card_status == "ACTIVE"
                and setup.enrollment_id is None)
        )
    )


def poll_setup(
    service_url: str,
    token: str,
    setup: Setup,
    *,
    attempts: int | None = None,
    deadline: float | None = None,
    expected_owner: str | None = None,
    expected_deployment: Setup | None = None,
    on_progress: Callable[[Setup], None] | None = None,
) -> Setup:
    """Observe and resume the saved setup within one monotonic deadline.

    A request failure escapes immediately: its write outcome may be unknown.
    Deadline exhaustion returns the latest confirmed state, never a new setup.
    """
    if deadline is None:
        deadline = time.monotonic() + SETUP_WAIT_SECONDS
    _check_owner(setup, expected_owner)
    if expected_deployment is not None:
        _check_deployment(setup, expected_deployment)
    owner = setup.owner_address or expected_owner
    owner_reported = setup.owner_address is not None
    completed = 0
    while setup.state == "pending" and (attempts is None or completed < attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        if time.monotonic() >= deadline:
            break
        setup = read_setup(service_url, token, deadline=deadline)
        if setup.state == "not_started":
            raise ServiceError("Setup response lost the saved setup.")
        _check_owner(setup, owner, required=owner_reported)
        if expected_deployment is not None:
            _check_deployment(setup, expected_deployment)
        if on_progress is not None:
            on_progress(setup)
        if _can_resume_setup(setup) and time.monotonic() < deadline:
            saved_owner = setup.owner_address
            setup = initialize_setup(service_url, token, saved_owner, deadline=deadline)
            _check_owner(setup, saved_owner, required=True)
            if expected_deployment is not None:
                _check_deployment(setup, expected_deployment)
            if on_progress is not None:
                on_progress(setup)
        if setup.owner_address is not None:
            owner = setup.owner_address
            owner_reported = True
        completed += 1
    return setup


FUNDING_PATH = "/kwal/participant/v1/funding"


FUNDING_STATES = frozenset({"setup_needed", "funds_needed", "processing", "ready", "error"})


# Available funds are the evidence of what can be spent, so a state that
# reports them cannot report them in part.
_FUNDED_STATES = frozenset({"funds_needed", "ready"})


@dataclasses.dataclass(frozen=True)
class FundingInstructions:
    chain_id: int
    token_address: str
    token_decimals: int
    usdc_faucet_url: str
    gas_faucet_url: str
    transfer_instructions: str


def _funding_instructions(body: Any) -> FundingInstructions | None:
    if body is None:
        return None
    if not isinstance(body, dict):
        raise ServiceError("Funding instructions are not an object.")
    # uint64 values use decimal strings in protobuf JSON.
    chain = body.get("chainId")
    chain_id = _whole_number(chain) if isinstance(chain, str) else None
    if chain_id is None or not 0 < chain_id < 2**64:
        raise ServiceError("Funding instructions have an invalid chain ID.")
    urls = {}
    for key in ("usdcFaucetUrl", "gasFaucetUrl"):
        value = _field_text(body, key, subject="Funding", required=True)
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError as error:
            raise ServiceError(
                "Funding instructions have an invalid faucet URL."
            ) from error
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password
        ):
            raise ServiceError("Funding instructions have an invalid faucet URL.")
        urls[key] = value
    # The deployed instruction paragraph is longer than other display fields.
    transfer = body.get("transferInstructions")
    if (
        not isinstance(transfer, str) or not transfer.strip()
        or not transfer.isprintable() or len(transfer) > 2000
    ):
        raise ServiceError("Funding instructions have invalid transfer text.")
    return FundingInstructions(
        chain_id=chain_id,
        token_address=_field_address(
            body, "tokenAddress", subject="Funding", required=True
        ),
        token_decimals=_field_count(
            body, "tokenDecimals", subject="Funding", required=False, maximum=36
        ) or 0,
        usdc_faucet_url=urls["usdcFaucetUrl"],
        gas_faucet_url=urls["gasFaucetUrl"],
        transfer_instructions=transfer,
    )


@dataclasses.dataclass(frozen=True)
class FundingToken:
    chain_id: int
    contract_address: str
    symbol: str
    decimals: int


@dataclasses.dataclass(frozen=True)
class QuoteFunding:
    quote_id: str
    merchant_total: Amount
    required: Amount
    token: FundingToken


def _quote_funding(body: dict[str, Any]) -> QuoteFunding | None:
    raw_quote = body.get("quoteFunding")
    if not isinstance(raw_quote, dict):
        return None
    quote_id = _field_text(raw_quote, "quoteId", subject="Quote funding", required=False)
    if quote_id is None:
        raise ServiceError("Quote funding response has no quote ID.")
    merchant = _amount(raw_quote, "merchantTotal", subject="Funding")
    required = _amount(body, "required", subject="Funding")
    raw_token = raw_quote.get("fundingToken")
    if merchant is None or required is None or not isinstance(raw_token, dict):
        raise ServiceError("Quote funding response has no amounts or token identity.")

    # proto3 JSON omits a zero, so an absent amount reads as zero. A quote
    # always costs something, so a zero total does not describe this quote and
    # must not tell the agent that the vault holds enough to check out.
    if merchant.minor_units <= 0 or required.minor_units <= 0:
        raise ServiceError("Quote funding response has no positive amounts.")
    chain_id = _whole_number(
        _field_text(raw_token, "chainId", subject="Funding token", required=True)
    )
    if chain_id is None or not 0 < chain_id <= 2**64 - 1:
        raise ServiceError("Funding token response has no valid chain ID.")
    token = FundingToken(
        chain_id=chain_id,
        contract_address=_field_address(
            raw_token, "contractAddress", subject="Funding token", required=True
        ),
        symbol=_field_text(raw_token, "symbol", subject="Funding token", required=True),
        decimals=_field_count(
            raw_token, "decimals", subject="Funding token", required=True, maximum=36
        ),
    )
    if (required.currency, required.decimals) != (token.symbol, token.decimals):
        raise ServiceError("Quote funding response has inconsistent token identity.")
    return QuoteFunding(quote_id, merchant, required, token)


@dataclasses.dataclass(frozen=True)
class Funding:
    state: str
    vault_address: str | None
    chain: str | None
    available: Amount | None
    shortfall: Amount | None
    required: Amount | None = None
    instructions: FundingInstructions | None = None
    quote: QuoteFunding | None = None


def parse_funding(body: Any) -> Funding:
    if not isinstance(body, dict):
        raise ServiceError("Funding response is not an object.")
    funding = Funding(
        state=_field_state(
            body, prefix="PARTICIPANT_FUNDING_STATE_",
            states=FUNDING_STATES, subject="Funding",
        ),
        vault_address=_field_address(
            body, "vaultAddress", subject="Funding", required=False
        ),
        chain=_field_text(body, "chain", subject="Funding", required=False),
        available=_amount(body, "available", subject="Funding"),
        shortfall=_amount(body, "shortfall", subject="Funding"),
        required=_amount(body, "required", subject="Funding"),
        instructions=_funding_instructions(body.get("instructions")),
        quote=_quote_funding(body),
    )
    # Older associations have no recorded vault address, even after a balance
    # read. A missing destination must not become an invented wallet address.
    if funding.vault_address is not None and (
        funding.chain is None or funding.state == "setup_needed"
    ):
        raise ServiceError("Funding response reports a vault without its setup or chain.")
    if funding.state not in _FUNDED_STATES and (
        funding.available is not None
        or funding.shortfall is not None
    ):
        raise ServiceError("Funding response reports unread funds with balance evidence.")
    amounts = [
        value for value in (funding.available, funding.shortfall, funding.required)
        if value is not None
    ]
    if any((value.currency, value.decimals) != ("USDC", 6) for value in amounts):
        raise ServiceError("Funding response amounts must use USDC with 6 decimals.")
    if funding.instructions and funding.instructions.token_decimals != 6:
        raise ServiceError("Funding instructions disagree with the amount precision.")
    if funding.shortfall is not None and funding.shortfall.minor_units == 0:
        funding = dataclasses.replace(funding, shortfall=None)
    if funding.state in _FUNDED_STATES and funding.available is None:
        raise ServiceError("Funding response reports no available funds.")
    if funding.state == "ready" and funding.shortfall is not None:
        raise ServiceError("Funding response reports ready with a shortfall.")
    if funding.state in _FUNDED_STATES:
        required = funding.required.minor_units if funding.required else 0
        available = funding.available.minor_units
        gap = max(required - available, 0)
        shortfall = funding.shortfall.minor_units if funding.shortfall else 0
        expected = (
            "ready" if available >= required and (required > 0 or available > 0)
            else "funds_needed"
        )
        if funding.state != expected or shortfall != gap:
            raise ServiceError("Funding response state disagrees with its amounts.")
    return funding


def _without_token_in_amount(amount: Amount | None, token: str) -> Amount | None:
    if amount is None:
        return None
    return _without_token(
        amount, token, subject="Funding", addresses=(), texts=("currency",)
    )


def _without_token_in_funding(funding: Funding, token: str) -> Funding:
    funding = _without_token(
        funding,
        token,
        subject="Funding",
        addresses=("vault_address",),
        texts=("chain",),
    )
    if funding.quote is not None:
        quote = _without_token(
            funding.quote, token, subject="Quote funding",
            addresses=(), texts=("quote_id",),
        )
        funding = dataclasses.replace(
            funding,
            quote=dataclasses.replace(
                quote,
                merchant_total=_without_token_in_amount(quote.merchant_total, token),
                required=_without_token_in_amount(quote.required, token),
                token=_without_token(
                    quote.token, token, subject="Funding token",
                    addresses=("contract_address",), texts=("symbol",),
                ),
            ),
        )
    return dataclasses.replace(
        funding,
        available=_without_token_in_amount(funding.available, token),
        shortfall=_without_token_in_amount(funding.shortfall, token),
        required=_without_token_in_amount(funding.required, token),
        instructions=(
            None if funding.instructions is None else _without_token(
                funding.instructions, token, subject="Funding",
                addresses=("token_address",),
                texts=("usdc_faucet_url", "gas_faucet_url", "transfer_instructions"),
            )
        ),
    )


def normalize_required_minor_units(value: str) -> str:
    """A manual balance check uses token units. Quote checks use the quote ID."""
    amount = _whole_number(value.strip())
    if amount is None or amount >= 2**64:
        raise ConfigurationError(
            "Required amount must be a whole number of minor units."
        )
    return str(amount)


def read_funding(
    service_url: str, token: str, *, required_minor_units: str | None = None,
    quote_id: str | None = None,
) -> Funding:
    """A read of readiness reserves nothing, so the request stays a GET."""
    if quote_id is not None and required_minor_units is not None:
        raise ConfigurationError("Choose a quote ID or a token amount.")
    path = FUNDING_PATH
    if quote_id is not None:
        path = f"{FUNDING_PATH}?{urllib.parse.urlencode({'quoteId': quote_id})}"
    elif required_minor_units is not None:
        query = urllib.parse.urlencode({"requiredMinorUnits": required_minor_units})
        path = f"{FUNDING_PATH}?{query}"
    body = request_json(service_url, path, token=token)
    funding = parse_funding(body)
    if quote_id is not None:
        # The backend prices the quote, so the helper cannot predict the echoed
        # amount and can only confirm that the answer names the asked quote.
        if funding.quote is None or funding.quote.quote_id != quote_id:
            raise ServiceError("Funding response does not match the requested quote.")
    else:
        echoed = funding.required.minor_units if funding.required is not None else None
        expected = (
            int(required_minor_units) if required_minor_units is not None else None
        )
        if echoed != expected:
            raise ServiceError("Funding response does not echo the requested amount.")
    return _without_token_in_funding(funding, token)


def poll_funding(
    service_url: str,
    token: str,
    funding: Funding,
    *,
    required_minor_units: str | None = None,
    quote_id: str | None = None,
    attempts: int = POLL_ATTEMPTS,
) -> Funding:
    """Polling is bounded, and a timeout returns the current state so a caller
    never asks for a second deposit and never creates a resource."""
    for _ in range(attempts):
        if funding.state != "processing":
            return funding
        time.sleep(POLL_INTERVAL_SECONDS)
        funding = read_funding(
            service_url, token,
            required_minor_units=required_minor_units, quote_id=quote_id,
        )
    return funding


def _check_owner(setup: Setup, owner: str | None, *, required: bool = False) -> None:
    if owner is not None and (
        (required and setup.owner_address is None)
        or (
            setup.owner_address is not None
            and setup.owner_address.lower() != owner.lower()
        )
    ):
        raise ServiceError("Setup response reports an owner address that was not requested.")


def _check_deployment(setup: Setup, expected: Setup) -> None:
    _check_owner(setup, expected.owner_address, required=True)
    if (
        (setup.vault_address or "").lower() != (expected.vault_address or "").lower()
        or setup.chain != expected.chain
        or (setup.deployment_tx_id or "").lower() != (expected.deployment_tx_id or "").lower()
    ):
        raise ServiceError("Setup response changed the recorded vault, chain, or deployment transaction.")


def _print_setup(setup: Setup) -> None:
    _print_setup_fields(setup)
    _print_setup_state(setup)


def _print_setup_fields(setup: Setup) -> None:
    for line in _setup_field_lines(setup):
        print(line)


def _setup_field_lines(setup: Setup) -> Iterator[str]:
    if setup.owner_address is not None:
        yield f"Owner address: {setup.owner_address}"
    else:
        yield "Owner address: not reported; owner match is unverified"
    if setup.card_status is not None:
        yield f"Sandbox card: {setup.card_status.lower()} (issuance, not enrollment)"
    if setup.enrollment_id is not None:
        yield f"Enrollment: {setup.enrollment_id} ({setup.enrollment_status.lower()})"
    if setup.vault_address is not None:
        yield f"Vault address: {setup.vault_address}"
    if setup.chain is not None:
        yield f"Chain: {setup.chain}"
    if setup.deployment_tx_id is not None:
        yield f"Deployment transaction: {setup.deployment_tx_id}"


def _print_setup_state(setup: Setup) -> None:
    if setup.state == "ready":
        print("Setup: ready for checkout")
        print("Next: run the funding command to check the test funds: python3 scripts/register.py funding")
        return
    print(f"Setup: processing{'' if setup.step is None else f' at {setup.step}'}")
    if setup.card_status == "ACTIVE":
        print(
            "Next: run the funding command to check funds and get any needed "
            "deposit instructions: python3 scripts/register.py funding."
            + (
                " Checkout still needs card enrollment."
                if setup.step == "card_enrollment" and setup.enrollment_status != "ACTIVE"
                else ""
            )
        )
        return
    print(
        "Next: resume the same setup: python3 scripts/register.py setup"
    )


def _command_status(args: argparse.Namespace, now: int) -> int:
    """One read of the saved setup. It never initializes or resumes a step, so
    a builder can run it at any time, including while a participant is stopped
    for operator help."""
    credentials = _session(args, now)
    setup = read_setup(credentials.service_url, credentials.token)
    if setup.state == "not_started":
        print("Setup: not started")
        print(
            "Next: ask the user for the vault owner address, then run: "
            "python3 scripts/register.py setup --owner-address <address>"
        )
        return 0
    if setup.state != "needs_operator":
        _print_setup(setup)
        if setup.state == "pending":
            print("To resume a supported step, run: python3 scripts/register.py setup")
        return 0
    _print_setup_fields(setup)
    print(f"Setup: stopped for operator help at {setup.step or 'an unreported step'}")
    print(
        "Next: report the step and ask the operator to resolve it for this "
        "participant. Funding checks stay unavailable until then; do not "
        "register again."
    )
    return 0


def _command_setup(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    deadline = time.monotonic() + SETUP_WAIT_SECONDS

    # Recovery commands must retain an explicitly selected participant.
    credential_option = (
        "" if args.credentials is None else f" --credentials {shlex.quote(args.credentials)}"
    )
    setup_command = f"python3 scripts/register.py setup{credential_option}"
    funding_command = f"python3 scripts/register.py funding{credential_option}"
    printed: set[str] = set()

    def report_progress(current: Setup) -> None:
        if current.state not in {"pending", "ready"}:
            return
        lines = list(_setup_field_lines(current))
        if current.state == "ready":
            lines.append("Setup: ready for checkout")
        else:
            lines.append(f"Setup: processing{'' if current.step is None else f' at {current.step}'}")
        if current.step == "deposit_observation":
            lines.append(f"Next: run the funding command for deposit instructions: {funding_command}")
        for line in lines:
            if line not in printed:
                print(line, flush=True)
                printed.add(line)

    # An unusable address is a mistake the user must hear about, whether or not
    # this run is the one that would send it.
    owner_address = (
        None
        if args.owner_address is None
        else normalize_owner_address(args.owner_address)
    )

    setup = read_setup(credentials.service_url, credentials.token, deadline=deadline)
    _check_owner(setup, owner_address)
    expected_deployment = None
    if args.reconcile_vault:
        if (
            setup.state != "needs_operator" or setup.step != "vault_deployment"
            or setup.owner_address is None or setup.vault_address is None
            or setup.chain is None or setup.deployment_tx_id is None
        ):
            raise ConfigurationError(
                "Vault reconciliation needs an operator-stopped vault deployment"
                " with its saved owner, vault, chain, and transaction hash."
            )
        expected_deployment = setup
        # One explicit request checks the saved transaction. Never retry a POST
        # or replace the participant, owner, vault, or transaction on uncertainty.
        if time.monotonic() < deadline:
            setup = initialize_setup(
                credentials.service_url, credentials.token, expected_deployment.owner_address,
                deadline=deadline,
            )
            _check_deployment(setup, expected_deployment)
    elif setup.state == "not_started":
        if owner_address is None:
            raise ConfigurationError("Setup has not started for this session.")
        if time.monotonic() < deadline:
            setup = initialize_setup(
                credentials.service_url, credentials.token, owner_address, deadline=deadline,
            )
    else:
        report_progress(setup)
        if _can_resume_setup(setup) and time.monotonic() < deadline:
            saved_owner = setup.owner_address
            setup = initialize_setup(
                credentials.service_url, credentials.token, saved_owner, deadline=deadline,
            )
            _check_owner(setup, saved_owner, required=True)
        elif owner_address is not None and setup.owner_address is None:
            print("Setup: the supplied owner address is not used; setup already started")
    _check_owner(setup, owner_address)
    report_progress(setup)
    setup = poll_setup(
        credentials.service_url, credentials.token, setup, expected_owner=owner_address,
        expected_deployment=expected_deployment, deadline=deadline, on_progress=report_progress,
    )
    if setup.state == "needs_operator":
        enrollment = (
            f" Enrollment: {setup.enrollment_id} ({setup.enrollment_status})."
            if setup.enrollment_id is not None else ""
        )
        raise ServiceError(
            f"Setup needs an operator at {setup.step or 'an unreported step'}.{enrollment}"
            f" After operator resolution, run: {setup_command}"
        )
    if setup.state == "ready":
        print(f"Next: run the funding command to check the test funds: {funding_command}")
    else:
        if setup.state == "not_started":
            setup_command += f" --owner-address {shlex.quote(owner_address)}"
        print(f"Setup wait limit reached ({SETUP_WAIT_SECONDS} seconds); setup is not ready.")
        print(f"Next: resume the same saved setup: {setup_command}")
        if setup.card_status == "ACTIVE" and setup.step == "card_enrollment":
            print("Checkout still needs card enrollment.")
    return 0


def _print_funding(
    funding: Funding,
    requested: str | None,
    *,
    purchase_next: str = "Next: start the purchase.",
) -> None:
    if funding.vault_address is not None:
        print(f"Vault address: {funding.vault_address}")
    if funding.chain is not None:
        print(f"Chain: {funding.chain}")
    for label, value in (
        ("Card-spendable", funding.available),
        ("Required", funding.required),
        ("Shortfall", funding.shortfall),
    ):
        if value is not None:
            print(f"{label}: {value}")
    # The funding API reports card spending capacity, not on-chain withdrawal
    # capacity. Neither a positive nor a zero card balance proves the latter.
    print("Withdrawable: unknown (operator required)")
    if funding.state == "error":
        print("Funding: blocked; operator action required")
        print(
            "Next: ask the operator to investigate. Do not transfer more funds to "
            "clear a block."
        )
        return
    if funding.state == "setup_needed":
        print("Funding: setup needed")
        print("Next: run the setup command to create the vault and the card.")
        return
    if funding.instructions is not None:
        instructions = funding.instructions
        print(f"Chain ID: {instructions.chain_id}")
        print(f"Token address: {instructions.token_address}")
        print(f"Token decimals: {instructions.token_decimals}")
        if funding.state == "funds_needed":
            print(f"USDC faucet: {instructions.usdc_faucet_url}")
            print(f"Gas faucet: {instructions.gas_faucet_url}")
            if funding.vault_address is not None and funding.chain is not None:
                print(f"Transfer instructions: {instructions.transfer_instructions}")
    if funding.state == "funds_needed":
        print("Funding: more funds needed")
        if funding.vault_address is None or funding.instructions is None:
            print(
                "Next: ask the operator for the missing vault destination or funding "
                "instructions before transferring."
            )
        else:
            print(
                "Next: transfer USDC to the printed vault using the configured token and "
                "chain. The sending wallet holds gas on the printed chain. No token "
                "approval is needed."
            )
        print(
            "After a submitted transfer, check the same vault. Do not send a second "
            "transfer solely because this state persists."
        )
        return
    if funding.state == "ready":
        print("Funding: ready for payment. Readiness is not a reservation.")
        if requested is None or int(requested) == 0:
            print(
                "Next: check funding with the required USDC amount before the purchase. "
                "Do not copy a quote in another currency or precision."
            )
            return
        print(purchase_next)
        return
    print("Funding: processing")
    print(
        "Next: report the state. Card-spendable funds are unknown; these destination "
        "details do not request another transfer. Check the same vault when the "
        "user asks to continue."
    )


def _command_funding(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)

    requested = None
    if args.required_minor_units is not None:
        requested = normalize_required_minor_units(args.required_minor_units)

    funding = read_funding(
        credentials.service_url, credentials.token, required_minor_units=requested
    )
    funding = poll_funding(
        credentials.service_url,
        credentials.token,
        funding,
        required_minor_units=requested,
    )
    _print_funding(funding, requested)
    return 0
