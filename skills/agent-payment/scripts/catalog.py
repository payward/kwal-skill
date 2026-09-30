"""Product search, product options, and variant selection."""

from __future__ import annotations

import argparse
import dataclasses
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from _fields import (
    _MAX_LIST_ENTRIES,
    _bounded_positive_integer,
    _field_flag,
    _field_objects,
    _field_text,
    _input_text,
    _object,
    _route_path,
    normalize_identifier,
)
from amounts import Amount, _amount
from cli_support import _price_text
from errors import ConfigurationError, ServiceError
from session import _session
from transport import _participant_json

PRODUCTS_PATH = "/kwal/participant/v1/products"


def normalize_search_query(value: str) -> str:
    return _input_text(value, subject="Search query")


def normalize_search_limit(value: str | int) -> int:
    return _bounded_positive_integer(
        value, subject="Search limit", maximum=_MAX_LIST_ENTRIES
    )


def normalize_option(value: str) -> tuple[str, str]:
    """One flag carries one selection, stated as name=value."""
    name, separator, selected = value.partition("=")
    if not separator:
        raise ConfigurationError("Option must be stated as name=value.")
    return (
        _input_text(name, subject="Option name"),
        _input_text(selected, subject="Option value"),
    )


@dataclasses.dataclass(frozen=True)
class Product:
    product_id: str
    title: str
    merchant: str | None
    price: Amount | None


@dataclasses.dataclass(frozen=True)
class ProductOptionValue:
    option_id: str
    label: str


@dataclasses.dataclass(frozen=True)
class ProductOption:
    name: str
    values: tuple[ProductOptionValue, ...]


@dataclasses.dataclass(frozen=True)
class ProductDetail:
    product: Product
    options: tuple[ProductOption, ...]


@dataclasses.dataclass(frozen=True)
class Variant:
    variant_id: str
    title: str | None
    price: Amount | None
    purchasable: bool


def _product(body: dict[str, Any], *, subject: str) -> Product:
    """A price the service does not report is reported as missing. A purchase
    decision needs the merchant's own price, so no price is inferred."""
    return Product(
        product_id=_field_text(body, "productId", subject=subject, required=True),
        title=_field_text(body, "title", subject=subject, required=True),
        merchant=_field_text(body, "merchant", subject=subject, required=False),
        price=_amount(body, "price", subject=subject),
    )


def parse_products(body: Any) -> tuple[Product, ...]:
    """An absent list is an empty result. A search without a match is reported as
    empty, and no product is invented."""
    subject = "Product search"
    return tuple(
        _product(entry, subject=subject)
        for entry in _field_objects(
            _object(body, subject=subject), "products", subject=subject
        )
    )


def parse_product_detail(body: Any) -> ProductDetail:
    """An option decides the variant, so an option without values cannot be
    offered to the user."""
    subject = "Product"
    detail = _object(body, subject=subject)
    options = []
    for entry in _field_objects(detail, "options", subject=subject):
        values = tuple(
            ProductOptionValue(
                option_id=_field_text(value, "optionId", subject=subject, required=True),
                label=_field_text(value, "label", subject=subject, required=True),
            )
            for value in _field_objects(entry, "values", subject=subject)
        )
        if not values:
            raise ServiceError(f"{subject} response reports an option without values.")
        options.append(
            ProductOption(
                name=_field_text(entry, "name", subject=subject, required=True),
                values=values,
            )
        )
    return ProductDetail(
        product=_product(
            _object(detail.get("product"), subject=subject), subject=subject
        ),
        options=tuple(options),
    )


def parse_variant(body: Any) -> Variant:
    """A purchasable variant carries the price the quote will use, so a
    purchasable variant without one is a contract mismatch."""
    subject = "Variant"
    stated = _object(body, subject=subject)
    variant = Variant(
        variant_id=_field_text(stated, "variantId", subject=subject, required=True),
        title=_field_text(stated, "title", subject=subject, required=False),
        price=_amount(stated, "price", subject=subject),
        purchasable=_field_flag(stated, "purchasable", subject=subject),
    )
    if variant.purchasable and (variant.price is None or not variant.price.minor_units):
        raise ServiceError(
            f"{subject} response reports a purchasable variant without a price."
        )
    return variant


def search_products(
    service_url: str, token: str, query: str, *, limit: int | None = None
) -> tuple[Product, ...]:
    fields: dict[str, Any] = {"query": query}
    if limit is not None:
        fields["limit"] = limit
    path = f"{PRODUCTS_PATH}?{urllib.parse.urlencode(fields)}"
    body = _participant_json(service_url, path, token, subject="Product search")
    return parse_products(body)


def read_product(service_url: str, token: str, product_id: str) -> ProductDetail:
    path = _route_path(PRODUCTS_PATH, product_id)
    return parse_product_detail(
        _participant_json(service_url, path, token, subject="Product")
    )


def resolve_variant(
    service_url: str,
    token: str,
    product_id: str,
    selected_options: tuple[tuple[str, str], ...],
) -> Variant:
    """Resolve human choices to the provider's published option ids. The
    service still chooses the variant; labels never become invented ids."""
    option_ids = []
    if selected_options:
        detail = read_product(service_url, token, product_id)
        selected_names = set()
        for name, label in selected_options:
            if name in selected_names:
                raise ConfigurationError(f"Option {name} was selected more than once.")
            selected_names.add(name)
            options = [option for option in detail.options if option.name == name]
            values = [
                value for option in options for value in option.values
                if value.label == label
            ]
            if len(options) != 1 or len(values) != 1:
                raise ConfigurationError(
                    f"Option {name}={label} does not match one published choice."
                    " Read the product options again."
                )
            option_ids.append(values[0].option_id)
        # The provider cannot resolve a partial selection and answers it as
        # unavailable, which reads as an outage. Name the gap before the request.
        missing = [option.name for option in detail.options if option.name not in selected_names]
        if missing:
            raise ConfigurationError(
                f"Option {', '.join(missing)} needs a selection. Repeat --option for every printed option."
            )
    payload = {"optionIds": option_ids}
    body = _participant_json(
        service_url,
        _route_path(PRODUCTS_PATH, product_id, "variant"),
        token,
        method="POST",
        payload=payload,
        subject="Variant",
    )
    return parse_variant(body)


def _print_product(product: Product) -> None:
    fields = [product.product_id, product.title]
    if product.merchant is not None:
        fields.append(product.merchant)
    fields.append(_price_text(product.price))
    print(" | ".join(fields))


def _command_products(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    limit = None
    if args.limit is not None:
        limit = normalize_search_limit(args.limit)

    products = search_products(
        credentials.service_url,
        credentials.token,
        normalize_search_query(args.query),
        limit=limit,
    )
    if not products:
        print("No products found.")
        # Reap can return no products for a search that finds them on
        # another attempt, so one empty result is not proof of absence.
        print(
            "Next: run the same search again, up to 2 more times. "
            "Then search with only the product name, without a brand or store name. "
            "If the search still finds nothing, ask the user for more detail."
        )
        return 0
    for product in products:
        _print_product(product)
    print("Next: read one product with the product command to see its options.")
    return 0


def _command_product(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    detail = read_product(
        credentials.service_url,
        credentials.token,
        normalize_identifier(args.product_id),
    )
    _print_product(detail.product)
    for option in detail.options:
        print(f"Option {option.name}: {', '.join(value.label for value in option.values)}")
    print(
        "Next: resolve the selection with the variant command, one --option"
        " for every printed option."
    )
    return 0


def _command_variant(args: argparse.Namespace, now: int) -> int:
    credentials = _session(args, now)
    variant = resolve_variant(
        credentials.service_url,
        credentials.token,
        normalize_identifier(args.product_id),
        tuple(normalize_option(option) for option in args.option),
    )
    print(f"Variant id: {variant.variant_id}")
    if variant.title is not None:
        print(f"Title: {variant.title}")
    if variant.price is not None:
        print(f"Price: {variant.price}")
    if not variant.purchasable:
        print("Variant: not purchasable")
        print("Next: ask the user for a different selection. Do not create a quote.")
        return 0
    print("Variant: purchasable")
    print("Next: create a quote for this variant.")
    return 0
