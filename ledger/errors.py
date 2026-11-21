"""Errors the API reports as RFC 9457 problem details."""

from __future__ import annotations

from typing import Any


class LedgerError(Exception):
    """A request the ledger refuses. ``code`` is stable and meant for clients to branch on."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def not_found(kind: str, ident: str) -> LedgerError:
    return LedgerError(
        404, f"{kind}_not_found", f"{kind.replace('_', ' ')} {ident!r} does not exist"
    )


def invalid(detail: str, **extra: Any) -> LedgerError:
    return LedgerError(400, "invalid_request", detail, **extra)


def rule(code: str, detail: str, **extra: Any) -> LedgerError:
    """A well-formed request that breaks a ledger rule."""
    return LedgerError(422, code, detail, **extra)


def conflict(code: str, detail: str, **extra: Any) -> LedgerError:
    """The request conflicts with the current state of a resource."""
    return LedgerError(409, code, detail, **extra)
