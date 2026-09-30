"""Quote pricing, shipping selection, and funding checks."""

from __future__ import annotations

import argparse
import dataclasses
import time
from typing import Any

from _fields import (
    _bounded_positive_integer,
    _field_count,
    _field_instant,
    _field_objects,
    _field_text,
    _input_text,
    _object,
    _route_path,
    normalize_identifier,
)
from amounts import Amount, _amount
from cli_support import _format_expiry, _price_text, _token_text
from errors import ConfigurationError, ServiceError
from session import Credentials, _session
from transport import _participant_json
from vault import _print_funding, poll_funding, read_funding

# A quantity is an agent input, and one UAT purchase stays small.
_MAX_QUANTITY = 100


# The contract states a quote line quantity as a proto3 uint32.
_MAX_UINT32 = 2**32 - 1


def normalize_quantity(value: str | int) -> int:
    return _bounded_positive_integer(value, subject="Quantity", maximum=_MAX_QUANTITY)


QUOTES_PATH = "/kwal/participant/v1/quotes"


ADDRESS_FIELDS = (
    ("firstName", "--first-name", True),
    ("lastName", "--last-name", True),
    ("phone", "--phone", True),
    ("line1", "--line1", True),
    ("line2", "--line2", False),
    ("city", "--city", True),
    ("region", "--region", False),
    ("postalCode", "--postal-code", False),
    ("country", "--country", True),
)


def normalize_shipping_address(stated: dict[str, str | None]) -> dict[str, str] | None:
    """The service prices shipping from the address, so a partial address is
    refused instead of being priced wrong. An item that needs no address needs
    no flags, so nothing stated sends nothing."""
    given = {key: value for key, value in stated.items() if value is not None}
    if not given:
        return None
    missing = [
        flag for key, flag, required in ADDRESS_FIELDS if required and key not in given
    ]
    if missing:
        raise ConfigurationError(f"Shipping address needs {' '.join(missing)}.")
    return {
        key: _input_text(given[key], subject="Shipping address")
        for key, _, _ in ADDRESS_FIELDS
        if key in given
    }


@dataclasses.dataclass(frozen=True)
class QuoteLine:
    variant_id: str
    quantity: int


@dataclasses.dataclass(frozen=True)
class ShippingOption:
    shipping_option_id: str
    label: str
    price: Amount | None


@dataclasses.dataclass(frozen=True)
class Quote:
    """The service owns the pricing. This record carries what the agent reviews
    and what decides the next step."""

    quote_id: str
    lines: tuple[QuoteLine, ...]
    shipping_options: tuple[ShippingOption, ...]
    selected_shipping_option_id: str | None
    subtotal: Amount | None
    shipping: Amount | None
    tax: Amount | None
    total: Amount
    expires_at: int
    payment_id: str | None = None

    def is_expired(self, now: int) -> bool:
        return now >= self.expires_at

    def needs_shipping_selection(self) -> bool:
        """An offered shipping option changes the total, so the total is not
        final until one is selected."""
        return bool(self.shipping_options) and self.selected_shipping_option_id is None


def _quote_lines(body: dict[str, Any], *, subject: str) -> tuple[QuoteLine, ...]:
    lines = []
    for entry in _field_objects(body, "lines", subject=subject):
        quantity = _field_count(
            entry, "quantity", subject=subject, required=True, maximum=_MAX_UINT32
        )
        if not quantity:
            raise ServiceError(f"{subject} response reports a line without a quantity.")
        lines.append(
            QuoteLine(
                variant_id=_field_text(
                    entry, "variantId", subject=subject, required=True
                ),
                quantity=quantity,
            )
        )
    if not lines:
        raise ServiceError(f"{subject} response reports no lines.")
    return tuple(lines)


def _shipping_options(
    body: dict[str, Any], *, subject: str
) -> tuple[ShippingOption, ...]:
    return tuple(
        ShippingOption(
            shipping_option_id=_field_text(
                entry, "shippingOptionId", subject=subject, required=True
            ),
            label=_field_text(entry, "label", subject=subject, required=True),
            price=_amount(entry, "price", subject=subject),
        )
        for entry in _field_objects(body, "shippingOptions", subject=subject)
    )


def parse_quote(body: Any) -> Quote:
    """The total is the amount funding must cover and the amount the checkout
    charges, so a quote without one cannot be reviewed. The parts are optional
    because proto3 JSON omits a zero."""
    subject = "Quote"
    quote = _object(body, subject=subject)
    options = _shipping_options(quote, subject=subject)
    selected = _field_text(
        quote, "selectedShippingOptionId", subject=subject, required=False
    )
    if selected is not None and selected not in {
        option.shipping_option_id for option in options
    }:
        raise ServiceError(
            f"{subject} response reports a selected shipping option that it"
            " does not offer."
        )
    total = _amount(quote, "total", subject=subject)
    if total is None or not total.minor_units:
        raise ServiceError(f"{subject} response reports no total.")
    return Quote(
        payment_id=_field_text(quote, "paymentId", subject=subject, required=False),
        quote_id=_field_text(quote, "quoteId", subject=subject, required=True),
        lines=_quote_lines(quote, subject=subject),
        shipping_options=options,
        selected_shipping_option_id=selected,
        subtotal=_amount(quote, "subtotal", subject=subject),
        shipping=_amount(quote, "shipping", subject=subject),
        tax=_amount(quote, "tax", subject=subject),
        total=total,
        expires_at=_field_instant(quote, "expiresAtUnixSeconds", subject=subject),
    )


def create_quote(
    service_url: str,
    token: str,
    lines: tuple[QuoteLine, ...],
    *,
    email: str,
    shipping_address: dict[str, str] | None = None,
) -> Quote:
    payload: dict[str, Any] = {
        "email": _input_text(email, subject="Buyer email"),
        "lines": [
            {"variantId": line.variant_id, "quantity": line.quantity} for line in lines
        ]
    }
    if shipping_address is not None:
        payload["shippingAddress"] = shipping_address
    return parse_quote(
        _participant_json(
            service_url,
            QUOTES_PATH,
            token,
            method="POST",
            payload=payload,
            subject="Quote",
        )
    )


def read_quote(service_url: str, token: str, quote_id: str) -> Quote:
    quote = parse_quote(
        _participant_json(
            service_url, _route_path(QUOTES_PATH, quote_id), token, subject="Quote"
        )
    )
    if quote.quote_id != quote_id:
        raise ServiceError("Quote response does not match the requested quote.")
    return quote


def select_shipping(
    service_url: str, token: str, quote_id: str, shipping_option_id: str
) -> Quote:
    """The service reprices the quote for the selection, so the returned quote
    replaces the one the agent read."""
    quote = parse_quote(
        _participant_json(
            service_url,
            _route_path(QUOTES_PATH, quote_id, "shipping"),
            token,
            method="POST",
            payload={"shippingOptionId": shipping_option_id},
            subject="Quote",
        )
    )
    if quote.quote_id != quote_id:
        raise ServiceError("Quote response does not match the requested quote.")
    if quote.selected_shipping_option_id != shipping_option_id:
        raise ServiceError("Quote response does not confirm the requested shipping option.")
    return quote


def _print_quote(quote: Quote, now: int) -> None:
    print(f"Quote id: {quote.quote_id}")
    if quote.quote_id.startswith("sandbox_quote_"):
        print("Sandbox quote: fixed demo pricing of 1 USDC per item.")
        print("No merchant order or shipping. Catalogue prices do not set this total.")
    if quote.payment_id is not None:
        print(f"Attached payment: {quote.payment_id}")
    for line in quote.lines:
        print(f"Item {line.variant_id} x{line.quantity}")
    if quote.subtotal is not None:
        print(f"Subtotal: {quote.subtotal}")
    if quote.shipping is not None:
        print(f"Shipping: {quote.shipping}")
    if quote.tax is not None:
        print(f"Tax: {quote.tax}")
    print(f"Total: {quote.total}")
    expiry = _format_expiry(quote.expires_at, now)
    print(f"Quote expires at: {quote.expires_at} ({expiry})")
    if quote.selected_shipping_option_id is not None:
        print(f"Selected shipping option: {quote.selected_shipping_option_id}")
    for option in quote.shipping_options:
        price = _price_text(option.price)
        print(f"Shipping option {option.shipping_option_id} | {option.label} | {price}")


def _ask_for_a_fresh_quote() -> int:
    print("Quote: expired")
    print(
        "Next: if no checkout was submitted, prepare a fresh quote with the"
        " same variant, quantity, delivery address, and shipping preference."
        " Review the new total and obtain purchase authorization."
        " Do not reuse this quote id."
    )
    return 0


def _quote_refusal(quote: Quote, now: int) -> int | None:
    """A quote that expired, or that still needs a shipping selection, is not
    a priced purchase, so every command that would act on it stops here."""
    if quote.payment_id is not None:
        source = "The sandbox" if quote.quote_id.startswith("sandbox_quote_") else "Reap"
        print(
            f"Next: read payment {quote.payment_id} with the payment command."
            f" Do not submit another checkout for this quote. {source} returns the"
            " same quote for the same item and quantity for about 15 minutes;"
            " to buy again, choose another item or quantity, or wait for a new"
            " quote."
        )
        return 0
    if quote.is_expired(now):
        return _ask_for_a_fresh_quote()
    if quote.needs_shipping_selection():
        print("Shipping: selection required")
        print(
            "Next: select one printed shipping option with the shipping"
            " command. The total is not final until then."
        )
        return 0
    return None


def _review_quote(credentials: Credentials, quote: Quote, now: int) -> int:
    """Every command that returns a quote reports the same next step, so the
    agent reads one decision wherever the quote came from."""
    _print_quote(quote, now)
    refusal = _quote_refusal(quote, now)
    if refusal is not None:
        return refusal

    try:
        funding = read_funding(
            credentials.service_url, credentials.token, quote_id=quote.quote_id
        )
        funding = poll_funding(
            credentials.service_url,
            credentials.token,
            funding,
            quote_id=quote.quote_id,
        )
    except ServiceError:
        # The backend refuses funding for an expired quote, so a quote that
        # ages out during the request explains the refusal. The user needs a
        # replacement quote, not a service diagnosis.
        if quote.is_expired(int(time.time())):
            return _ask_for_a_fresh_quote()
        raise
    if funding.quote is None:
        raise ServiceError("Funding response has no quote amounts.")
    required = funding.quote.required
    requested = str(required.minor_units)
    total_label = "Demo total" if quote.quote_id.startswith("sandbox_quote_") else "Merchant total"
    print(f"{total_label}: {funding.quote.merchant_total}")
    print(f"Required funding: {required}")
    print(
        f"Funding token: {funding.quote.token.contract_address}"
        f" on chain {funding.quote.token.chain_id}"
    )
    if quote.is_expired(int(time.time())):
        return _ask_for_a_fresh_quote()
    for priced in (funding.available, funding.shortfall):
        if priced is not None and (priced.currency, priced.decimals) != (
            required.currency,
            required.decimals,
        ):
            raise ServiceError(
                f"Funding response prices {_token_text(priced)} while funding"
                f" requires {_token_text(required)}."
            )
    _print_funding(
        funding,
        requested,
        purchase_next=(
            f"Next: hand quote id {quote.quote_id} and this total to the"
            " checkout command."
            if funding.quote.merchant_total == quote.total
            else "Next: the quote price changed. Review the current merchant total"
            " and obtain purchase authorization before checkout."
        ),
    )
    return 0


def _stated_address(args: argparse.Namespace) -> dict[str, str] | None:
    return normalize_shipping_address(
        {
            key: getattr(args, flag[2:].replace("-", "_"))
            for key, flag, _ in ADDRESS_FIELDS
        }
    )


def _command_quote(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    line = QuoteLine(
        variant_id=normalize_identifier(args.variant),
        quantity=normalize_quantity(args.quantity),
    )
    quote = create_quote(
        credentials.service_url,
        credentials.token,
        (line,),
        email=args.email,
        shipping_address=_stated_address(args),
    )
    return _review_quote(credentials, quote, now)


def _command_quote_check(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    quote = read_quote(
        credentials.service_url,
        credentials.token,
        normalize_identifier(args.quote_id),
    )
    return _review_quote(credentials, quote, now)


def _command_shipping(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    quote_id = normalize_identifier(args.quote_id)
    option_id = normalize_identifier(args.option)
    # Resume a reserved payment and avoid posting an already-selected option:
    # Reap's sandbox can reject a redundant selection as unavailable shipping.
    current = read_quote(credentials.service_url, credentials.token, quote_id)
    if (
        current.payment_id is not None
        or current.is_expired(now)
        or current.selected_shipping_option_id == option_id
    ):
        return _review_quote(credentials, current, now)
    quote = select_shipping(
        credentials.service_url,
        credentials.token,
        quote_id,
        option_id,
    )
    return _review_quote(credentials, quote, now)
