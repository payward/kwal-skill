"""Session registration, private credential storage, and session commands."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from _fields import _whole_number
from cli_support import _command_line, _format_expiry, _report
from errors import AgentPaymentError, ConfigurationError, CredentialsError, ServiceError
from transport import normalize_service_url, request_json, resolve_service_url

CREDENTIALS_FILE_ENV = "PWS_CREDENTIALS_FILE"


# The routes are the published Kwal participant v1 contract.
REGISTER_PATH = "/kwal/participant/v1/register"


_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR


_OWNER_ONLY_DIRECTORY = stat.S_IRWXU


_GROUP_AND_OTHER = stat.S_IRWXG | stat.S_IRWXO


def _optional_home() -> Path | None:
    try:
        home = Path.home()
    except RuntimeError:
        return None
    # A relative home directory would put the default path under the working
    # directory, which changes with every caller. An empty HOME resolves to the
    # root, which holds no private configuration directory.
    if not home.is_absolute() or home == Path(home.anchor):
        return None
    return home


def _checkout_containing(path: Path) -> Path | None:
    for directory in path.parents:
        if (directory / ".git").exists():
            return directory
    return None


def resolve_credentials_path(explicit: str | None = None) -> Path:
    selected = (
        explicit if explicit is not None else os.environ.get(CREDENTIALS_FILE_ENV)
    )
    if selected is not None:
        # A caller that names a destination must not get the default one.
        candidate = selected.strip()
        if not candidate:
            raise ConfigurationError("Credentials path is empty")
        path = Path(candidate).expanduser()
        # A relative path depends on the working directory, which can put the
        # token in a checkout that this helper cannot see in advance.
        if not path.is_absolute():
            raise ConfigurationError(f"Credentials path {candidate} must be absolute")
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home and not Path(config_home).expanduser().is_absolute():
            raise ConfigurationError("XDG_CONFIG_HOME must be an absolute path")
        if config_home:
            base = Path(config_home).expanduser()
        else:
            home = _optional_home()
            if home is None:
                raise ConfigurationError(
                    "No home directory. Set "
                    f"{CREDENTIALS_FILE_ENV} to an absolute path."
                )
            base = home / ".config"
        path = base / "pws" / "agent-payment" / "credentials.json"

    return refuse_a_checkout_path(path)


def refuse_a_checkout_path(path: Path) -> Path:
    # A symlinked parent would otherwise put the token inside a checkout.
    try:
        resolved = path.parent.resolve() / path.name
    # A symlink loop in the path is reported as a loop or as a runtime error,
    # depending on the interpreter.
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"Credentials path {path} is not usable") from error

    # A parent that exists but is not a directory, such as a symlink loop or a
    # dangling link, cannot hold the token. The command must fail here, before
    # the service issues a session that nothing can save.
    if os.path.lexists(resolved.parent) and not os.path.isdir(resolved.parent):
        raise ConfigurationError(f"Credentials path {path} is not usable")

    if _checkout_containing(resolved) is not None:
        raise ConfigurationError(
            "Credentials must be stored outside a repository checkout. "
            f"Set {CREDENTIALS_FILE_ENV} to a path under your config directory."
        )
    return resolved


def _session_fields(
    raw: Any,
    *,
    token_key: str,
    expiry_key: str,
    subject: str,
    failure: type[AgentPaymentError],
) -> tuple[str, int]:
    """Both trust boundaries carry the same two fields under different names."""
    if not isinstance(raw, dict):
        raise failure(f"{subject} is not a JSON object.")

    token = raw.get(token_key)
    if not isinstance(token, str) or not token.strip():
        raise failure(f"{subject} has no {token_key}.")
    # The token becomes a request header, where a control character would
    # either split the request or raise an error that quotes the token.
    if not token.isascii() or not token.isprintable() or " " in token:
        raise failure(f"{subject} has an unusable {token_key}.")

    expires_at = raw.get(expiry_key)
    # proto3 JSON states a 64-bit integer as a string, while the saved file
    # states the same instant as a number.
    if isinstance(expires_at, str):
        expires_at = _whole_number(expires_at)
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise failure(f"{subject} has no integer {expiry_key}.")

    return token, expires_at


@dataclasses.dataclass(frozen=True)
class Credentials:
    """`token` is secret: never print it and never write it to a log."""

    service_url: str
    token: str = dataclasses.field(repr=False)
    expires_at: int

    def is_expired(self, now: int) -> bool:
        return now >= self.expires_at


def parse_registration(service_url: str, body: Any, now: int) -> Credentials:
    token, expires_at = _session_fields(
        body,
        token_key="token",
        expiry_key="expiresAtUnixSeconds",
        subject="Registration response",
        failure=ServiceError,
    )
    if expires_at <= now:
        raise ServiceError("Registration returned an already expired session.")

    return Credentials(service_url=service_url, token=token, expires_at=expires_at)


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Credentials:
        subject = f"Credentials at {self.path}"
        # The walk refuses a link in any component, so the path is resolved
        # first and the pinned directory then holds still while it is read.
        try:
            parent = self._open_checked_directory(self.path.parent.resolve())
        except FileNotFoundError as error:
            raise CredentialsError(
                f"No credentials at {self.path}. Register explicitly first."
            ) from error
        except (OSError, RuntimeError) as error:
            raise CredentialsError(
                f"Could not read credentials at {self.path}."
            ) from error

        try:
            document = self._read_private(subject, parent)
        finally:
            os.close(parent)

        try:
            raw = json.loads(document)
        except json.JSONDecodeError as error:
            raise CredentialsError(
                f"Could not read credentials at {self.path}."
            ) from error

        token, expires_at = _session_fields(
            raw,
            token_key="token",
            expiry_key="expires_at",
            subject=subject,
            failure=CredentialsError,
        )

        service_url = raw.get("service_url")
        if not isinstance(service_url, str):
            raise CredentialsError(f"{subject} has no service_url.")
        try:
            service_url = normalize_service_url(service_url)
        except ConfigurationError as error:
            raise CredentialsError(f"{subject} has an unusable service_url.") from error

        return Credentials(service_url=service_url, token=token, expires_at=expires_at)

    def _read_private(self, subject: str, parent: int) -> str:
        # The checks and the read must see one inode, so the file is opened
        # once from the pinned directory and then inspected through that
        # descriptor.
        try:
            descriptor = os.open(
                self.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent
            )
        except FileNotFoundError as error:
            raise CredentialsError(
                f"No credentials at {self.path}. Register explicitly first."
            ) from error
        except OSError as error:
            raise CredentialsError(
                f"Could not read credentials at {self.path}."
            ) from error

        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CredentialsError(f"{subject} is not a regular file.")
            if stat.S_IMODE(info.st_mode) & _GROUP_AND_OTHER:
                raise CredentialsError(
                    f"{subject} is readable by other users. "
                    "Restore owner-only access before you use the session."
                )
            try:
                return handle.read().decode("utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise CredentialsError(
                    f"Could not read credentials at {self.path}."
                ) from error

    def refuse_an_unusable_destination(self, *, overwrite: bool) -> None:
        # The service issues a session that only this file can hold, so an
        # entry the save can neither create nor replace must stop the command
        # before a token is issued.
        subject = f"Could not save credentials at {self.path}"
        for ancestor in (self.path.parent, *self.path.parent.parents):
            if not os.path.lexists(ancestor):
                continue
            if not ancestor.is_dir():
                raise CredentialsError(f"{subject}: {ancestor} is not a directory.")
            break

        if os.path.isdir(self.path) and not self.path.is_symlink():
            raise CredentialsError(f"{subject}: it is a directory.")

    def save(self, credentials: Credentials, *, overwrite: bool = False) -> None:
        # The replacement is staged in a private file and then moved into place,
        # so a failed write can never destroy a session that still works.
        staged = f"{self.path.name}.{os.getpid()}.new"
        try:
            # The descriptor pins the checked directory, so a symlink swapped
            # in after the check cannot redirect the token to another place.
            parent = self._open_checked_directory(self._prepare_directory())
            try:
                self._write_private(staged, credentials, parent)
                if overwrite:
                    os.replace(
                        staged, self.path.name, src_dir_fd=parent, dst_dir_fd=parent
                    )
                else:
                    self._link_without_replacing(staged, parent)
                # The staged data becomes durable only once its entry is.
                os.fsync(parent)
            finally:
                try:
                    os.unlink(staged, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)
        except OSError as error:
            raise CredentialsError(
                f"Could not save credentials at {self.path}: {error.strerror}"
            ) from error

    def _prepare_directory(self) -> Path:
        # Every directory this helper creates is owner-only, and a directory
        # that already exists keeps its mode because it may be shared with
        # unrelated configuration.
        missing = [
            directory
            for directory in (self.path.parent, *self.path.parent.parents)
            if not directory.exists()
        ]
        for directory in reversed(missing):
            directory.mkdir(mode=_OWNER_ONLY_DIRECTORY)
            # The process umask removes bits from the requested mode, so the
            # mode is set again to keep the token reachable by its owner.
            directory.chmod(_OWNER_ONLY_DIRECTORY)

        # The path was validated before the directories existed, so the
        # completed tree is checked again against any checkout.
        return refuse_a_checkout_path(self.path).parent

    def _open_checked_directory(self, validated: Path) -> int:
        # Every component is opened from the root without following a link, so
        # a symlink swapped into any parent after the check cannot redirect the
        # token to another place.
        descriptor = os.open(validated.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in validated.relative_to(validated.anchor).parts:
                below = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = below
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _write_private(
        self, staged: str, credentials: Credentials, parent: int
    ) -> None:
        descriptor = os.open(
            staged,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _OWNER_ONLY,
            dir_fd=parent,
        )
        # The process umask removes bits from the requested mode, so the mode
        # is set again to keep the token readable by its owner.
        os.fchmod(descriptor, _OWNER_ONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "service_url": credentials.service_url,
                    "token": credentials.token,
                    "expires_at": credentials.expires_at,
                },
                handle,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _link_without_replacing(self, staged: str, parent: int) -> None:
        try:
            os.link(staged, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
        except FileExistsError as error:
            raise CredentialsError(
                f"Credentials already exist at {self.path}. "
                "Use a different --credentials path for a separate participant."
            ) from error


def register(service_url: str, now: int) -> Credentials:
    # A JSON-only route can reject a POST that carries no body at all.
    body = request_json(service_url, REGISTER_PATH, method="POST", payload={})
    return parse_registration(service_url, body, now)


def _expired_session(expires_at: int) -> CredentialsError:
    return CredentialsError(
        f"Session expired at {expires_at}. "
        "There is no session renewal. Preserve the credentials and contact the operator."
    )


def _session(args: argparse.Namespace, now: int) -> Credentials:
    credentials = CredentialStore(resolve_credentials_path(args.credentials)).load()
    if credentials.is_expired(now):
        raise _expired_session(credentials.expires_at)
    return credentials


def _command_register(args: argparse.Namespace, now: int) -> int:
    # Reporting a saved session must not need a service URL.
    store = CredentialStore(resolve_credentials_path(args.credentials))

    # A dangling symlink is an entry that the save cannot replace, so it must
    # stop the command before the service issues a session that nothing holds.
    if args.force:
        raise CredentialsError(
            "Credentials replacement is unsupported. Registration creates a separate "
            "participant; use a new --credentials path and preserve the existing file."
        )
    store.refuse_an_unusable_destination(overwrite=False)
    if store.path.exists() or store.path.is_symlink():
        current = store.load()
        # An expired saved session is not usable, so a failing command reports
        # nothing on stdout that a calling agent can read as success.
        if current.is_expired(now):
            raise _expired_session(current.expires_at)
        print(f"Credentials already saved at {store.path}.")
        print(f"Session {_format_expiry(current.expires_at, now)}.")
        print("Use a new --credentials path only to create a separate participant.")
        return 0

    service_url = resolve_service_url(args.service_url)
    credentials = register(service_url, now)
    store.save(credentials)

    print(f"Registered with {credentials.service_url}.")
    print(f"Credentials saved to {store.path} (owner read/write only).")
    print(f"Session {_format_expiry(credentials.expires_at, now)}.")
    return 0


def _command_show(args: argparse.Namespace, now: int) -> int:
    store = CredentialStore(resolve_credentials_path(args.credentials))
    credentials = store.load()
    if credentials.is_expired(now):
        raise _expired_session(credentials.expires_at)

    print(f"Credentials file: {store.path}")
    print(f"Service URL: {credentials.service_url}")
    print(
        f"Expires at: {credentials.expires_at} ({_format_expiry(credentials.expires_at, now)})"
    )
    return 0


def _command_check(args: argparse.Namespace, now: int) -> int:
    problems: list[str] = []

    try:
        print(f"Service URL: {resolve_service_url(args.service_url)}")
    except AgentPaymentError as error:
        problems.append(str(error))

    path: Path | None = None
    try:
        path = resolve_credentials_path(args.credentials)
        print(f"Credentials file: {path}")
    except AgentPaymentError as error:
        problems.append(str(error))

    if path is not None:
        try:
            credentials = CredentialStore(path).load()
            if credentials.is_expired(now):
                problems.append(str(_expired_session(credentials.expires_at)))
            else:
                print(f"Session {_format_expiry(credentials.expires_at, now)}.")
        except AgentPaymentError as error:
            problems.append(str(error))

    # Every problem is reported in one run, because a caller that fixes one
    # value at a time needs a second run to see the next one.
    for problem in problems:
        _report(problem, credentials=args.credentials)
    if problems:
        return 1

    print("Local configuration is usable. The service was not called.")
    print(f"Next: read the vault and card state with: {_command_line('status', args.credentials)}")
    return 0


def _request_payload(body_file: str | None) -> dict | list | None:
    if body_file is None:
        return None
    try:
        raw = Path(body_file).read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(
            f"Could not read the request body at {body_file}."
        ) from error
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"The request body at {body_file} is not JSON."
        ) from error


def _command_call(args: argparse.Namespace, now: int) -> int:
    if not args.path.startswith("/"):
        raise ConfigurationError(f"Request path {args.path} must start with a slash.")

    credentials = _session(args, now)

    body = request_json(
        credentials.service_url,
        args.path,
        method=args.method,
        payload=_request_payload(args.body_file),
        token=credentials.token,
    )
    json.dump(body, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0
