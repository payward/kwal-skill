"""Exact token amounts and their participant response fields."""

from __future__ import annotations

import dataclasses
from typing import Any

from _fields import _field_count, _field_text, _whole_number
from errors import ServiceError

# An amount is printed in whole units. The unit bound is the uint256 range an
# ERC-20 balance can hold, and the exponent bound keeps one printed amount
# inside a terminal line.
_MAX_TOKEN_DECIMALS = 36


_MAX_TOKEN_UNITS = 2**256 - 1


def _field_minor_units(body: dict[str, Any], key: str, *, subject: str) -> int:
    """The contract states minor units as text, which keeps the value exact
    for any token decimals a float would round. proto3 JSON omits a zero, so
    an absent field is that zero."""
    value = _field_text(body, key, subject=subject, required=False)
    if value is None:
        return 0
    units = _whole_number(value)
    if units is None or units > _MAX_TOKEN_UNITS:
        raise ServiceError(f"{subject} response field {key} is not a whole amount.")
    return units


def format_units(units: int, decimals: int) -> str:
    """The service states exact minor units, so the display shifts the decimal
    point as text and never through a float."""
    digits = str(units).rjust(decimals + 1, "0")
    if decimals == 0:
        return digits
    return f"{digits[:-decimals]}.{digits[-decimals:]}"


@dataclasses.dataclass(frozen=True)
class Amount:
    minor_units: int
    currency: str
    decimals: int

    def __str__(self) -> str:
        return f"{format_units(self.minor_units, self.decimals)} {self.currency}"


def _amount(body: dict[str, Any], key: str, *, subject: str) -> Amount | None:
    """proto3 JSON omits a default scalar, so an unset amount and a zero
    amount both arrive without a currency. Neither is an amount to report."""
    value = body.get(key, {})
    if not isinstance(value, dict):
        raise ServiceError(f"{subject} response field {key} is not an amount.")
    currency = _field_text(value, "currency", subject=subject, required=False)
    if currency is None:
        return None
    return Amount(
        minor_units=_field_minor_units(value, "minorUnits", subject=subject),
        currency=currency,
        decimals=_field_count(
            value,
            "decimals",
            subject=subject,
            required=False,
            maximum=_MAX_TOKEN_DECIMALS,
        )
        or 0,
    )
