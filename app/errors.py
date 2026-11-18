"""Domain level errors mapped to stable HTTP error envelopes."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """An expected, client-facing failure.

    The API renders this as::

        {"error": {"code": "...", "message": "...", "details": {...}}}
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
