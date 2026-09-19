"""Compatibility import for the canonical API client."""

from clients.clinic_client import (
    ATTEMPTS,
    READ_ATTEMPTS,
    RETRY_DELAY_SECS,
    RETRYABLE_STATUSES,
    SUBMIT_ROUTES,
    ClinicApiError,
    ClinicClient,
    DryRunSubmit,
)

__all__ = [
    "ATTEMPTS",
    "READ_ATTEMPTS",
    "RETRY_DELAY_SECS",
    "RETRYABLE_STATUSES",
    "SUBMIT_ROUTES",
    "ClinicApiError",
    "ClinicClient",
    "DryRunSubmit",
]
