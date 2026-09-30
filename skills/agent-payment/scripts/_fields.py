"""Shared input and response checks for the participant contract."""

from __future__ import annotations

import dataclasses
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TypeVar

from errors import ConfigurationError, ServiceError


def _whole_number(text: str) -> int | None:
    """A number in the contract is ASCII text. `str.isdecimal` also accepts
    another script's digits, which `int` then converts, so an unreadable value
    would reach the human as a number they cannot compare."""
    if not text.isascii() or not text.isdecimal():
        return None
    return int(text)


_ADDRESS_DIGITS = frozenset("0123456789abcdefABCDEF")


_ADDRESS_LENGTH = 42


_ZERO_ADDRESS = f"0x{'0' * 40}"


# A step or a reason is printed, so an unbounded or non-printable value from
# the service must never reach the terminal.
_MAX_DISPLAY_CHARACTERS = 200


# One printed list must stay inside a terminal screen, so every repeated
# field shares one bound.
_MAX_LIST_ENTRIES = 50


_REDACTED = "[redacted]"


_RecordT = TypeVar("_RecordT")


def _shaped_address(address: str) -> bool:
    return (
        address.startswith("0x")
        and len(address) == _ADDRESS_LENGTH
        and set(address[2:]) <= _ADDRESS_DIGITS
        and address.lower() != _ZERO_ADDRESS
    )


def _text(value: Any, *, key: str, subject: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(f"{subject} response field {key} is not text.")
    if not value.isprintable() or len(value) > _MAX_DISPLAY_CHARACTERS:
        raise ServiceError(f"{subject} response field {key} is not displayable.")
    return value


def _field_text(
    body: dict[str, Any], key: str, *, subject: str, required: bool
) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    return _text(value, key=key, subject=subject)


def _field_address(
    body: dict[str, Any], key: str, *, subject: str, required: bool
) -> str | None:
    """An address from the service is shown as a funding destination, so a
    value that cannot be an address is a contract mismatch, not a label."""
    address = _field_text(body, key, subject=subject, required=required)
    if address is not None and not _shaped_address(address):
        raise ServiceError(f"{subject} response field {key} is not an address.")
    return address


def _field_state(
    body: dict[str, Any], *, prefix: str, states: frozenset[str], subject: str
) -> str:
    """The contract states an enum by its full name. The short name is what the
    agent and the commands work with, so the prefix is dropped here."""
    value = _field_text(body, "state", subject=subject, required=True)
    state = value[len(prefix) :].lower() if value.startswith(prefix) else ""
    if state not in states:
        raise ServiceError(f"{subject} response carries an unknown state.")
    return state


def _field_enum(
    body: dict[str, Any], key: str, *, prefix: str, values: frozenset[str], subject: str
) -> str | None:
    """The contract states an enum by its full name, while the short name is
    what the agent shows. An unspecified value is the contract's absence."""
    value = _field_text(body, key, subject=subject, required=False)
    if value is None or value == f"{prefix}UNSPECIFIED":
        return None
    name = value[len(prefix) :] if value.startswith(prefix) else ""
    if name not in values:
        raise ServiceError(f"{subject} response carries an unknown {key}.")
    return name


def _without_token(
    record: _RecordT,
    token: str,
    *,
    subject: str,
    addresses: tuple[str, ...],
    texts: tuple[str, ...],
) -> _RecordT:
    """The service holds the bearer token, so any text it chooses must be
    cleared of the token before the agent prints it."""
    # An opaque token can be address-shaped, and an address is printed as it
    # arrives, so an address that carries the token is a contract mismatch.
    for name in addresses:
        address = getattr(record, name)
        if address is not None and token in address:
            raise ServiceError(
                f"{subject} response reports the session token in an address."
            )
    return dataclasses.replace(
        record,
        **{name: _redacted(getattr(record, name), token) for name in texts},
    )


def _redacted(text: str | None, token: str) -> str | None:
    return None if text is None else text.replace(token, _REDACTED)


def _field_count(
    body: dict[str, Any], key: str, *, subject: str, required: bool, maximum: int
) -> int | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ServiceError(f"{subject} response field {key} is not a whole amount.")
    return value


def _field_flag(body: dict[str, Any], key: str, *, subject: str) -> bool:
    """proto3 JSON omits a false, so an absent flag is that false."""
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ServiceError(f"{subject} response field {key} is not a flag.")
    return value


def _field_list(body: dict[str, Any], key: str, *, subject: str) -> list[Any]:
    """proto3 JSON omits an empty repeated field, so an absent list is empty."""
    values = body.get(key, [])
    if not isinstance(values, list):
        raise ServiceError(f"{subject} response field {key} is not a list.")
    if len(values) > _MAX_LIST_ENTRIES:
        raise ServiceError(f"{subject} response field {key} reports too many entries.")
    return values


def _field_texts(body: dict[str, Any], key: str, *, subject: str) -> tuple[str, ...]:
    return tuple(
        _text(value, key=key, subject=subject)
        for value in _field_list(body, key, subject=subject)
    )


def _field_objects(
    body: dict[str, Any], key: str, *, subject: str
) -> list[dict[str, Any]]:
    values = _field_list(body, key, subject=subject)
    for value in values:
        if not isinstance(value, dict):
            raise ServiceError(
                f"{subject} response field {key} carries an entry that is not"
                " an object."
            )
    return values


def _input_text(value: str, *, subject: str) -> str:
    text = value.strip()
    if not text or not text.isprintable() or len(text) > _MAX_DISPLAY_CHARACTERS:
        raise ConfigurationError(f"{subject} must be printable text.")
    return text


def normalize_identifier(value: str) -> str:
    """An identifier travels back to the service in a request path or body, so
    the helper passes it through and refuses only what cannot be one."""
    identifier = _input_text(value, subject="Identifier")
    if not identifier.isascii() or " " in identifier:
        raise ConfigurationError("Identifier must be ASCII text without spaces.")
    return identifier


def _bounded_positive_integer(value: str | int, *, subject: str, maximum: int) -> int:
    """argparse exits with its own message and no recovery action, so the helper
    parses the number itself and reports it like every other input error."""
    if isinstance(value, str):
        stated = value.strip()
        if not stated.isdecimal():
            raise ConfigurationError(f"{subject} must be a whole number.")
        value = int(stated)
    if not 1 <= value <= maximum:
        raise ConfigurationError(f"{subject} must be between 1 and {maximum}.")
    return value


def _object(body: Any, *, subject: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ServiceError(f"{subject} response is not a JSON object.")
    return body


def _route_path(base: str, identifier: str, *suffix: str) -> str:
    """An identifier reaches the request path, so it is escaped there and
    never joined raw."""
    return "/".join((base, urllib.parse.quote(identifier, safe=""), *suffix))


# The contract states an instant as a proto3 int64.
_MAX_INT64 = 2**63 - 1


def _field_instant(
    body: dict[str, Any], key: str, *, subject: str, required: bool = True
) -> int | None:
    """proto3 JSON states a 64-bit integer as a string and omits a zero. Every
    instant in the contract is a deadline, so a zero would read as no deadline
    and a value above the contract range as a distant one. The agent would then
    carry an expired quote or an expired approval link to the human."""
    value = body.get(key)
    if value is None and not required:
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= _MAX_INT64
    ):
        raise ServiceError(f"{subject} response field {key} is not an instant.")
    return value
