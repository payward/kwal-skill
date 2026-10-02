"""HTTP requests with bounded responses and no credential redirects."""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from errors import ConfigurationError, ServiceError

# The public UAT gateway is the default, so a builder needs no host to start.
# The variable points the same helper at another gateway or a local fixture.
SERVICE_URL_ENV = "PWS_SERVICE_URL"
DEFAULT_SERVICE_URL = "https://api.sandbox.services.payward.com"


_HOST_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._:")


# The gateway terminates TLS. Loopback is allowed only so the helper can be
# exercised against a local fixture server without a certificate.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


_REQUEST_TIMEOUT_SECONDS = 30


# A registration response carries two short fields. Anything larger is a
# misrouted or hostile service, so it is refused before it is decoded.
_MAX_RESPONSE_BYTES = 64 * 1024


# Only stable participant error tags declared by the API may reach the terminal.
# Titles, data, provider messages and unknown types can contain private values.
_ERROR_TAGS = frozenset({
    "ParticipantFundingRequest",
    "ParticipantCardBusy",
    "ParticipantBadRequest",
    "ParticipantUnauthenticated",
    "ParticipantNotFound",
    "ParticipantUnavailable",
    "ParticipantRegistrationRejected",
    "ParticipantRegistrationRateLimited",
    "ParticipantRegistrationNeedsOperator",
})
_ERROR_TYPES = {f"tag:kraken.com,2025:{tag}": tag for tag in _ERROR_TAGS}

# A busy card names the caller's own holding payment, so that one data field
# may reach the terminal. It must still look like a payment id the service
# accepts, because the agent passes it to the next command.
_PAYMENT_ID = re.compile(r"(?!\.+$)[A-Za-z0-9._~-]{1,128}")


def _holding_payment(body: dict, token: str | None) -> str | None:
    data = body.get("data")
    holder = data.get("holdingPaymentId") if isinstance(data, dict) else None
    if (
        isinstance(holder, str)
        and _PAYMENT_ID.fullmatch(holder)
        and not (token and token in holder)
    ):
        return holder
    return None


def _error_diagnostics(error: urllib.error.HTTPError, token: str | None) -> str:
    """Best-effort diagnostics must not replace the original HTTP failure."""
    details = []
    trace = error.headers.get("x-trace-id") if error.headers else None
    if (
        isinstance(trace, str)
        and re.fullmatch(r"[0-9a-f]{32}", trace)
        and not (token and token in trace)
    ):
        details.append(f"trace={trace[:8]}...{trace[-6:]}")
    try:
        raw = error.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) <= _MAX_RESPONSE_BYTES:
            body = json.loads(raw)
            error_type = body.get("type") if isinstance(body, dict) else None
            tag = _ERROR_TYPES.get(error_type) if isinstance(error_type, str) else None
            if tag and not (token and token in tag):
                details.insert(0, f"service_error={tag}")
                holder = _holding_payment(body, token) if tag == "ParticipantCardBusy" else None
                if holder:
                    details.insert(1, f"holding_payment={holder}")
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        pass
    return f" ({'; '.join(details)})" if details else ""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would forward the bearer token to an unverified origin."""

    def redirect_request(self, *args, **kwargs) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


# A request carries the token in a header, so the cleartext loopback exception
# must never be sent to a proxy that the environment configures.
_DIRECT_OPENER = urllib.request.build_opener(
    _RefuseRedirects, urllib.request.ProxyHandler({})
)


def normalize_service_url(service_url: str) -> str:
    candidate = service_url.strip().rstrip("/")
    if not candidate:
        raise ConfigurationError(f"{SERVICE_URL_ENV} is empty")

    try:
        parsed = urllib.parse.urlparse(candidate)
        # urlparse defers authority validation until the port is read.
        hostname, _port = parsed.hostname, parsed.port
    except ValueError as error:
        raise ConfigurationError(f"{SERVICE_URL_ENV} is not a usable URL") from error

    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and hostname in LOOPBACK_HOSTS
    ):
        raise ConfigurationError(
            f"{SERVICE_URL_ENV} must be an https URL; got scheme "
            f"{parsed.scheme!r} for host {hostname!r}"
        )
    if not hostname:
        raise ConfigurationError(f"{SERVICE_URL_ENV} must name a host")
    # A documented placeholder such as <gateway-host> reaches here when it
    # is copied literally, and the failure must name the configuration.
    if set(hostname) - _HOST_CHARACTERS:
        raise ConfigurationError(
            f"{SERVICE_URL_ENV} must name a real host; got {hostname!r}"
        )

    # Userinfo can send the bearer token to a different origin, and a path,
    # query or fragment would displace the fixed route appended to this URL.
    if (
        parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{SERVICE_URL_ENV} must be a scheme, host and optional port only"
        )
    # The origin is rebuilt, so an empty query or fragment delimiter cannot
    # survive into the URL that carries the fixed route.
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def resolve_service_url(explicit: str | None = None) -> str:
    if explicit is not None:
        return normalize_service_url(explicit)
    return normalize_service_url(os.environ.get(SERVICE_URL_ENV) or DEFAULT_SERVICE_URL)


def request_json(
    service_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    deadline: float | None = None,
) -> Any:
    """`service_url` must already be normalized."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")

    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{service_url}{path}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    loopback = urllib.parse.urlparse(url).hostname in LOOPBACK_HOSTS
    opener = _DIRECT_OPENER if loopback else _OPENER
    timeout = _REQUEST_TIMEOUT_SECONDS
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError(f"{method} {path} was not sent: request deadline reached.")
        # urllib's timeout bounds socket operations, not the entire response.
        # Cap each call by the command's remaining budget and check again
        # before any subsequent request or sleep.
        timeout = min(timeout, remaining)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        try:
            diagnostics = _error_diagnostics(error, token)
        finally:
            error.close()
        raise ServiceError(
            f"{method} {path} failed with HTTP {error.code}.{diagnostics}"
        ) from error
    # A reset or truncating peer raises while the body is read, not only
    # while the connection is made.
    except (OSError, http.client.HTTPException) as error:
        raise ServiceError(f"{method} {path} did not complete.") from error

    if not raw:
        raise ServiceError(f"{method} {path} returned an empty body.")
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ServiceError(f"{method} {path} returned too much data.")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceError(f"{method} {path} did not return JSON.") from error


# Every unsettled state is read again on the same subject, so one bound
# keeps a helper command from waiting on the service without an end.
POLL_ATTEMPTS = 10


POLL_INTERVAL_SECONDS = 3


def _participant_json(
    service_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    subject: str,
) -> Any:
    """A catalog or quote response carries identifiers that travel back to the
    service and text that reaches the terminal. A redaction would corrupt an
    identifier, so a response that repeats the session token is a contract
    mismatch and not a value to clean."""
    body = request_json(service_url, path, method=method, payload=payload, token=token)
    if _repeats_token(body, token):
        raise ServiceError(f"{subject} response reports the session token.")
    return body


def _repeats_token(body: Any, token: str) -> bool:
    """A serialized body escapes quotes and backslashes, so a token that holds
    either would not be found in the serialization. The parsed values carry the
    token exactly as the service sent it."""
    if isinstance(body, str):
        return token in body
    if isinstance(body, dict):
        return any(token in key for key in body) or any(
            _repeats_token(value, token) for value in body.values()
        )
    if isinstance(body, list):
        return any(_repeats_token(value, token) for value in body)
    return False
