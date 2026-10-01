#!/usr/bin/env python3
"""Parse and dispatch agent-payment commands."""

from __future__ import annotations

import argparse
import sys
import time

from catalog import _command_product, _command_products, _command_variant
from checkout import _command_checkout, _command_payment
from cli_support import _command_line, _report
from errors import AgentPaymentError, ConfigurationError, ServiceError
from quotes import (
    ADDRESS_FIELDS,
    _command_quote,
    _command_quote_check,
    _command_shipping,
)
from session import (
    CREDENTIALS_FILE_ENV,
    _command_call,
    _command_check,
    _command_register,
    _command_show,
)
from transport import DEFAULT_SERVICE_URL, SERVICE_URL_ENV
from vault import _command_funding, _command_setup, _command_status

_SERVICE_URL_HELP = (
    f"participant API base URL (default: ${SERVICE_URL_ENV}, then {DEFAULT_SERVICE_URL})"
)


class _Parser(argparse.ArgumentParser):
    """argparse exits with its own message and no recovery action, so the parser
    raises the failure like every other input error."""

    def error(self, message: str) -> None:
        raise ConfigurationError(f"Command input is not valid: {message}.")


def _build_parser() -> _Parser:
    parser = _Parser(description="Register the agent with the payment service.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser(
        "register", help="create a sandbox participant and save its session"
    )
    register_parser.add_argument(
        "--service-url",
        help=_SERVICE_URL_HELP,
    )
    register_parser.add_argument(
        "--force",
        action="store_true",
        help="unsupported: use a new --credentials path for a separate participant",
    )

    show_parser = subparsers.add_parser(
        "show", help="report saved credential metadata without the token"
    )

    check_parser = subparsers.add_parser(
        "check", help="report the configuration and the saved session state"
    )
    check_parser.add_argument(
        "--service-url",
        help=_SERVICE_URL_HELP,
    )

    status_parser = subparsers.add_parser(
        "status", help="read the vault and card setup state without changing it"
    )

    setup_parser = subparsers.add_parser(
        "setup", help="create or check the vault and the sandbox card"
    )
    setup_parser.add_argument(
        "--owner-address", help="vault owner address, for example 0x1234..."
    )

    setup_parser.add_argument(
        "--reconcile-vault",
        action="store_true",
        help="check one recorded deployment transaction without broadcasting another",
    )

    funding_parser = subparsers.add_parser(
        "funding", help="check test funding and payment readiness"
    )
    funding_parser.add_argument(
        "--required-minor-units",
        help="verified purchase funding amount in USDC minor units, for example 1500000",
    )

    products_parser = subparsers.add_parser(
        "products", help="search the participant product catalog"
    )
    products_parser.add_argument(
        "--query", required=True, help="search words, for example cotton shirt"
    )
    products_parser.add_argument("--limit", help="maximum number of products to print")

    product_parser = subparsers.add_parser(
        "product", help="read one product and the options that decide its variant"
    )
    product_parser.add_argument("product_id", help="product identifier")

    variant_parser = subparsers.add_parser(
        "variant", help="resolve the selected options to one purchasable variant"
    )
    variant_parser.add_argument("product_id", help="product identifier")
    variant_parser.add_argument(
        "--option",
        action="append",
        default=[],
        help="one selection, stated as Name=Value; repeat for every option",
    )

    quote_parser = subparsers.add_parser(
        "quote", help="create a quote for one purchasable variant"
    )
    quote_parser.add_argument("--variant", required=True, help="variant identifier")
    quote_parser.add_argument("--email", required=True, help="buyer contact email")
    quote_parser.add_argument(
        "--quantity", default=1, help="number of items (default: 1)"
    )
    for _, flag, required in ADDRESS_FIELDS:
        need = "needed with any other address flag" if required else "optional"
        quote_parser.add_argument(
            flag,
            help=f"shipping address {flag[2:].replace('-', ' ')} ({need})",
        )

    quote_check_parser = subparsers.add_parser(
        "quote-check", help="read a quote and price the funding readiness for it"
    )
    quote_check_parser.add_argument("quote_id", help="quote identifier")

    shipping_parser = subparsers.add_parser(
        "shipping", help="select one offered shipping option for a quote"
    )
    shipping_parser.add_argument("quote_id", help="quote identifier")
    shipping_parser.add_argument(
        "--option", required=True, help="shipping option identifier"
    )

    checkout_parser = subparsers.add_parser(
        "checkout", help="submit one prepared quote to the hosted checkout"
    )
    checkout_parser.add_argument("quote_id", help="quote identifier")

    payment_parser = subparsers.add_parser(
        "payment", help="observe one submitted checkout and report its outcome"
    )
    payment_parser.add_argument(
        "payment_id",
        nargs="?",
        help="payment identifier (default: the last recorded attempt)",
    )

    call_parser = subparsers.add_parser(
        "call", help="call a protected route with the saved session"
    )
    call_parser.add_argument(
        "path", help="route path, for example /kwal/participant/v1/payments"
    )
    call_parser.add_argument(
        "--method", default="GET", help="HTTP method (default: GET)"
    )
    call_parser.add_argument("--body-file", help="JSON request body file")

    for subparser in (
        register_parser,
        show_parser,
        check_parser,
        status_parser,
        setup_parser,
        funding_parser,
        products_parser,
        product_parser,
        variant_parser,
        quote_parser,
        quote_check_parser,
        shipping_parser,
        checkout_parser,
        payment_parser,
        call_parser,
    ):
        subparser.add_argument(
            "--credentials",
            help=f"credentials file path (default: ${CREDENTIALS_FILE_ENV})",
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    now = int(time.time())

    handlers = {
        "register": _command_register,
        "show": _command_show,
        "check": _command_check,
        "status": _command_status,
        "setup": _command_setup,
        "funding": _command_funding,
        "products": _command_products,
        "product": _command_product,
        "variant": _command_variant,
        "quote": _command_quote,
        "quote-check": _command_quote_check,
        "shipping": _command_shipping,
        "checkout": _command_checkout,
        "payment": _command_payment,
        "call": _command_call,
    }
    args = argparse.Namespace(command=None, credentials=None)
    try:
        args = _build_parser().parse_args(argv)
        return handlers[args.command](args, now)
    except AgentPaymentError as error:
        next_step = None
        if isinstance(error, ServiceError) and args.command == "setup":
            next_step = (
                "Before any retry, read the same saved setup without resuming: "
                f"{_command_line('status', args.credentials)}"
            )
        _report(str(error), next_step=next_step, credentials=args.credentials)
        return 1


if __name__ == "__main__":
    sys.exit(main())
