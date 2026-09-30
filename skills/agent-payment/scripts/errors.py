"""Failures with messages that are safe to show to the user."""

from __future__ import annotations


class AgentPaymentError(Exception):
    """A failure with a message safe to report to the user."""


class ConfigurationError(AgentPaymentError):
    pass


class ServiceError(AgentPaymentError):
    pass


class CredentialsError(AgentPaymentError):
    pass
