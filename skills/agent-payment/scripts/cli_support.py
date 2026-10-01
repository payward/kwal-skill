"""Command output and recovery instructions."""

from __future__ import annotations

import re
import shlex
import sys

from amounts import Amount

# The action for a failure belongs next to the failure, so a caller reports the
# same next step wherever the message appears.
_ACTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("PWS_SERVICE_URL",),
        "Unset PWS_SERVICE_URL to use the default UAT gateway, or export a "
        "valid gateway URL. See references/setup.md.",
    ),
    (
        (
            "XDG_CONFIG_HOME",
            "Credentials path",
            "No home directory",
            "Credentials must be stored",
        ),
        "Ask the user for a writable path outside every Git checkout. Export it as "
        "PWS_CREDENTIALS_FILE. See references/setup.md.",
    ),
    (
        ("No credentials at",),
        "Ask the user to approve a registration, then run: "
        "{register}",
    ),
    (
        ("Credentials already exist at", "Credentials replacement"),
        "Preserve the existing credentials. Only for an authorized separate "
        "participant, register with a new --credentials path.",
    ),
    (
        ("Could not read credentials at", "Credentials at"),
        "Preserve the file and repair its access or restore the same participant's "
        "credentials. If that is not possible, contact the operator; do not register again.",
    ),
    (
        ("Could not save credentials at",),
        "Preserve any credential files. If registration was sent, ask the operator "
        "to reconcile its outcome before another registration; do not retry it.",
    ),
    (
        ("Session expired",),
        "Preserve the credentials and contact the operator. Renewal is unsupported; "
        "registration creates a separate participant and does not recover its vault or card.",
    ),
    (
        ("Registration", "POST /kwal/participant/v1/register"),
        "Stop and ask the operator to reconcile registration before another attempt. "
        "Registration is not idempotent; do not retry after an uncertain outcome.",
    ),
    (
        (
            "Command input is not valid",
            "Request path",
            "Could not read the request body",
            "The request body",
            "Required amount",
            "Identifier",
            "Search query",
            "Search limit",
            "Quantity",
            "Option",
            "Shipping address",
            "Buyer email",
        ),
        "Correct the command input and run it again.",
    ),
    (
        ("Owner address", "Setup has not started"),
        "Ask the user for the vault owner address, then run: "
        "{setup}",
    ),
    (
        ("Setup already uses",),
        "Use the saved owner address; the command never replaces it.",
    ),
    (
        ("Setup needs an operator",),
        "Report the step to the user. Ask the operator to resolve the recorded "
        "outcome, then run this command again; do not register again.",
    ),
    (
        (
            "Could not read the payment record",
            "Could not save the payment record",
            "Could not lock or save the payment record",
            "The payment record",
        ),
        "Report the payment record as unusable. Do not submit a checkout again. "
        "Ask the user for the last payment id, then run: "
        "{payment}",
    ),
    (
        (
            "Setup response",
            "Funding response",
            "Product search response",
            "Product response",
            "Variant response",
            "Quote response",
            "Payment response",
            "GET /kwal/",
            "POST /kwal/",
        ),
        "Report the service error to the user. Ask the service operator to verify "
        "the API contract.",
    ),
)


# A service failure carries a stable participant error tag. The tag names the
# cause, so it decides the next step before the route prefix does. Registration
# keeps its own rule: any failed registration may have created a participant.
_SERVICE_ERROR_ACTIONS: dict[str, str] = {
    "ParticipantUnavailable": (
        "Read the setup state with: {status}. If it "
        "reports an operator stop, report the step and ask the operator to resolve "
        "it for this participant; do not register again. Otherwise follow the "
        "retry limit in references/debug.md#retries."
    ),
    "ParticipantNotFound": (
        "Check the id. Use an id printed by this skill for the same credentials, "
        "then run the command again. A quote or payment from another participant "
        "is not found."
    ),
    "ParticipantUnauthenticated": (
        "The service did not accept the saved session. Run: {check}. "
        "Preserve the credentials and contact the operator; do not register "
        "again to recover this participant."
    ),
    "ParticipantBadRequest": (
        "Correct the command input with values printed by the last product, variant, "
        "or quote command, and run it again."
    ),
    "ParticipantFundingRequest": (
        "The service refused to price this quote for funding. An expired quote needs "
        "a new quote: see references/quotes.md. This sandbox funds only USD quotes "
        "with Ink Sepolia USDC."
    ),
}

# Setup takes one input, the owner address, and the service refuses an address
# that another participant already owns.
_SETUP_BAD_REQUEST_ACTION = (
    "The service refused the owner address. Each participant needs its own owner "
    "address; an address used by another participant is refused. Read the state "
    "with: {status}. If it reports no owner, ask the "
    "user for an owner address that no other participant uses and run setup "
    "again. If it reports an owner, contact the operator; do not register again."
)

_SHIPPING_BAD_REQUEST_ACTION = (
    "Read the quote with: {quote_check}. "
    "If it has no attached payment, is unexpired and your option is already "
    "selected, use that quote and total. Otherwise choose an available option, "
    "or follow references/quotes.md "
    "for an expired quote or an attached payment. Do not retry the same rejected "
    "selection."
)

_RETRY_ACTION = (
    "Follow the retry limit in references/debug.md#retries. If the failure "
    "persists, report the error line and the trace to the operator."
)

_SERVICE_ERROR = re.compile(r"service_error=(\w+)")
_GATEWAY_FAILURE = re.compile(r"^(GET|POST) \S+ (failed with HTTP 50[234]\.|did not complete\.)")


def _command_line(command: str, credentials: str | None = None) -> str:
    """A copied next step must keep an explicitly selected participant."""
    option = "" if credentials is None else f" --credentials {shlex.quote(credentials)}"
    return f"python3 scripts/register.py {command}{option}"


def _action_template_for(message: str) -> str:
    if not message.startswith("POST /kwal/participant/v1/register"):
        tag = _SERVICE_ERROR.search(message)
        if (
            tag
            and tag.group(1) == "ParticipantBadRequest"
            and message.startswith("POST /kwal/participant/v1/initialize")
        ):
            return _SETUP_BAD_REQUEST_ACTION
        if (
            tag
            and tag.group(1) == "ParticipantBadRequest"
            and re.match(r"^POST /kwal/participant/v1/quotes/[^/ ]+/shipping ", message)
        ):
            return _SHIPPING_BAD_REQUEST_ACTION
        if tag and tag.group(1) in _SERVICE_ERROR_ACTIONS:
            return _SERVICE_ERROR_ACTIONS[tag.group(1)]
        if not tag and _GATEWAY_FAILURE.match(message):
            return _RETRY_ACTION
    for prefixes, action in _ACTIONS:
        if message.startswith(prefixes):
            return action
    return "Report the error line to the user and stop."


def _action_for(message: str, *, credentials: str | None = None) -> str:
    return _action_template_for(message).format(
        register=_command_line("register", credentials),
        setup=_command_line("setup --owner-address <address>", credentials),
        payment=_command_line("payment <payment-id>", credentials),
        status=_command_line("status", credentials),
        check=_command_line("check", credentials),
        quote_check=_command_line("quote-check <quote-id>", credentials),
    )


def _report(
    error: str, *, next_step: str | None = None, credentials: str | None = None,
) -> None:
    print(f"error: {error}", file=sys.stderr)
    action = _action_for(error, credentials=credentials)
    if next_step is not None:
        action += f" {next_step}"
    print(f"action: {action}", file=sys.stderr)


def _format_expiry(expires_at: int, now: int) -> str:
    remaining = expires_at - now
    if remaining <= 0:
        return "expired"
    days, rest = divmod(remaining, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"valid for {days}d {hours}h"
    if hours:
        return f"valid for {hours}h {rest // 60}m"
    return f"valid for {rest // 60}m"


def _token_text(priced: Amount) -> str:
    """The decimals decide what the minor units mean, so a mismatch report
    names them next to the currency."""
    return f"{priced.currency} with {priced.decimals} decimals"


def _price_text(price: Amount | None) -> str:
    """A purchase needs the merchant's own price, so an absent price is stated
    instead of leaving the column empty."""
    return "no price" if price is None else str(price)
