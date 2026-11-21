"""HTTP layer: routing, request parsing, idempotency keys and problem-detail errors."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import schemas
from .clock import Clock, SystemClock
from .db import Database, DatabaseBusy, migrate
from .errors import LedgerError, invalid, rule
from .service import Ledger

log = logging.getLogger("ledger.access")

IDEMPOTENCY_TTL_MS = 24 * 3600 * 1000
MAX_BODY_BYTES = 64 * 1024
ERROR_DOCS = "https://github.com/ik-786/ledger-service/blob/main/docs/errors.md"
DEFAULT_PAGE = 25
MAX_PAGE = 100

Model = TypeVar("Model", bound=schemas.Body)
Action = Callable[[sqlite3.Connection, Any], tuple[int, dict[str, Any]]]


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def json_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(dumps(payload), status, headers=headers, media_type="application/json")


class LedgerAPI:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self.ledger = Ledger(clock)

    # ----------------------------------------------------------------- plumbing

    async def _body(self, request: Request, model: type[Model]) -> tuple[Model, str]:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise LedgerError(413, "body_too_large", f"request body is over {MAX_BODY_BYTES} bytes")
        if raw.strip() and request.headers.get("content-type", "").split(";")[0].strip() != (
            "application/json"
        ):
            raise LedgerError(415, "unsupported_media_type", "send the body as application/json")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            raise invalid("request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise invalid("request body must be a JSON object")
        try:
            parsed = model.model_validate(data)
        except ValidationError as exc:
            errors = [
                {"field": ".".join(str(p) for p in err["loc"]) or None, "message": err["msg"]}
                for err in exc.errors(include_url=False, include_context=False, include_input=False)
            ]
            raise invalid("request body failed validation", errors=errors) from None
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        fingerprint = f"{request.method} {request.url.path}\n{canonical}"
        return parsed, hashlib.sha256(fingerprint.encode()).hexdigest()

    async def _mutate(self, request: Request, model: type[Model], action: Action) -> Response:
        """Run a write. With an Idempotency-Key, the effect and the stored response
        commit together, and a retry with the same key and body gets the stored
        response back instead of a second effect."""
        body, request_hash = await self._body(request, model)
        key = request.headers.get("idempotency-key")
        if key is not None and not (1 <= len(key) <= 255 and key.isprintable()):
            raise invalid("Idempotency-Key must be 1 to 255 printable characters")

        def work() -> tuple[int, bytes, bool]:
            with self.db.write() as conn:
                now = self.clock.now_ms()
                if key is not None:
                    stored = conn.execute(
                        "SELECT request_hash, status_code, response_body, created_at"
                        " FROM idempotency_keys WHERE key = ?",
                        (key,),
                    ).fetchone()
                    if stored is not None and now - stored["created_at"] >= IDEMPOTENCY_TTL_MS:
                        conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
                        stored = None
                    if stored is not None:
                        if stored["request_hash"] != request_hash:
                            raise rule(
                                "idempotency_key_reused",
                                "this Idempotency-Key was already used with a different request",
                            )
                        return stored["status_code"], stored["response_body"].encode(), True
                status, payload = action(conn, body)
                encoded = dumps(payload)
                if key is not None:
                    conn.execute(
                        "INSERT INTO idempotency_keys"
                        " (key, request_hash, status_code, response_body, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (key, request_hash, status, encoded.decode(), now),
                    )
                return status, encoded, False

        status, encoded, replayed = await run_in_threadpool(work)
        headers = {"Idempotent-Replayed": "true"} if replayed else None
        return Response(encoded, status, headers=headers, media_type="application/json")

    async def _read(self, fn: Callable[[sqlite3.Connection], dict[str, Any]]) -> Response:
        def work() -> dict[str, Any]:
            with self.db.read() as conn:
                return fn(conn)

        return json_response(200, await run_in_threadpool(work))

    @staticmethod
    def _page_args(request: Request) -> tuple[int, str | None]:
        raw = request.query_params.get("limit", str(DEFAULT_PAGE))
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_PAGE:
            raise invalid(f"limit must be an integer from 1 to {MAX_PAGE}")
        return int(raw), request.query_params.get("cursor") or None

    # ------------------------------------------------------------------ handlers

    async def health(self, request: Request) -> Response:
        def ping() -> dict[str, Any]:
            self.db.connection().execute("SELECT 1").fetchone()
            return {"status": "ok"}

        return json_response(200, await run_in_threadpool(ping))

    async def accounts(self, request: Request) -> Response:
        # One route per path, so a 405 lists every allowed method in its Allow header.
        if request.method == "POST":
            return await self.create_account(request)
        return await self.list_accounts(request)

    async def create_account(self, request: Request) -> Response:
        def action(
            conn: sqlite3.Connection, b: schemas.CreateAccount
        ) -> tuple[int, dict[str, Any]]:
            return 201, self.ledger.create_account(conn, b.name, b.currency, b.allow_negative)

        return await self._mutate(request, schemas.CreateAccount, action)

    async def list_accounts(self, request: Request) -> Response:
        limit, cursor = self._page_args(request)
        return await self._read(lambda conn: self.ledger.list_accounts(conn, limit, cursor))

    async def get_account(self, request: Request) -> Response:
        account_id = request.path_params["account_id"]
        return await self._read(lambda conn: self.ledger.get_account(conn, account_id))

    async def list_entries(self, request: Request) -> Response:
        account_id = request.path_params["account_id"]
        limit, cursor = self._page_args(request)
        return await self._read(
            lambda conn: self.ledger.list_entries(conn, account_id, limit, cursor)
        )

    async def transfer(self, request: Request) -> Response:
        def action(conn: sqlite3.Connection, b: schemas.Transfer) -> tuple[int, dict[str, Any]]:
            return 201, self.ledger.transfer(
                conn, b.from_account_id, b.to_account_id, b.amount, b.description
            )

        return await self._mutate(request, schemas.Transfer, action)

    async def journal(self, request: Request) -> Response:
        def action(conn: sqlite3.Connection, b: schemas.Journal) -> tuple[int, dict[str, Any]]:
            legs = [(leg.account_id, leg.amount) for leg in b.legs]
            return 201, self.ledger.post_journal(conn, legs, b.description)

        return await self._mutate(request, schemas.Journal, action)

    async def get_transaction(self, request: Request) -> Response:
        txn_id = request.path_params["txn_id"]
        return await self._read(lambda conn: self.ledger.get_transaction(conn, txn_id))

    async def reverse(self, request: Request) -> Response:
        txn_id = request.path_params["txn_id"]

        def action(conn: sqlite3.Connection, b: schemas.Reverse) -> tuple[int, dict[str, Any]]:
            return 201, self.ledger.reverse(conn, txn_id, b.description)

        return await self._mutate(request, schemas.Reverse, action)

    async def create_hold(self, request: Request) -> Response:
        def action(conn: sqlite3.Connection, b: schemas.CreateHold) -> tuple[int, dict[str, Any]]:
            return 201, self.ledger.create_hold(
                conn, b.account_id, b.destination_account_id, b.amount, b.expires_in_seconds
            )

        return await self._mutate(request, schemas.CreateHold, action)

    async def get_hold(self, request: Request) -> Response:
        hold_id = request.path_params["hold_id"]
        return await self._read(lambda conn: self.ledger.get_hold(conn, hold_id))

    async def capture(self, request: Request) -> Response:
        hold_id = request.path_params["hold_id"]

        def action(conn: sqlite3.Connection, b: schemas.Capture) -> tuple[int, dict[str, Any]]:
            return 200, self.ledger.capture_hold(conn, hold_id, b.amount)

        return await self._mutate(request, schemas.Capture, action)

    async def void(self, request: Request) -> Response:
        hold_id = request.path_params["hold_id"]

        def action(conn: sqlite3.Connection, b: schemas.Empty) -> tuple[int, dict[str, Any]]:
            return 200, self.ledger.void_hold(conn, hold_id)

        return await self._mutate(request, schemas.Empty, action)

    def routes(self) -> list[Route]:
        return [
            Route("/healthz", self.health, methods=["GET"]),
            Route("/v1/accounts", self.accounts, methods=["GET", "POST"]),
            Route("/v1/accounts/{account_id}", self.get_account, methods=["GET"]),
            Route("/v1/accounts/{account_id}/entries", self.list_entries, methods=["GET"]),
            Route("/v1/transfers", self.transfer, methods=["POST"]),
            Route("/v1/transactions", self.journal, methods=["POST"]),
            Route("/v1/transactions/{txn_id}", self.get_transaction, methods=["GET"]),
            Route("/v1/transactions/{txn_id}/reverse", self.reverse, methods=["POST"]),
            Route("/v1/holds", self.create_hold, methods=["POST"]),
            Route("/v1/holds/{hold_id}", self.get_hold, methods=["GET"]),
            Route("/v1/holds/{hold_id}/capture", self.capture, methods=["POST"]),
            Route("/v1/holds/{hold_id}/void", self.void, methods=["POST"]),
        ]


# ----------------------------------------------------------------------- errors


def problem(request: Request, status: int, code: str, detail: str, **extra: Any) -> Response:
    """RFC 9457 problem details, plus a stable ``code`` and the request id."""
    body = {
        "type": f"{ERROR_DOCS}#{code}",
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "code": code,
        "detail": detail,
        "request_id": getattr(request.state, "request_id", None),
        **extra,
    }
    headers = {"Retry-After": "1"} if status == 503 else None
    return Response(dumps(body), status, headers=headers, media_type="application/problem+json")


async def ledger_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, LedgerError)
    return problem(request, exc.status, exc.code, exc.detail, **exc.extra)


async def http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    codes = {404: "route_not_found", 405: "method_not_allowed"}
    response = problem(
        request, exc.status_code, codes.get(exc.status_code, "http_error"), exc.detail
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def busy_error(request: Request, exc: Exception) -> Response:
    return problem(request, 503, "database_busy", "the ledger is busy; retry shortly")


async def integrity_error(request: Request, exc: Exception) -> Response:
    # Reached only if a database constraint catches what the service checks missed.
    log.error("integrity error: %s", exc)
    return problem(request, 409, "constraint_violation", "the request conflicts with stored data")


async def unexpected_error(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error", exc_info=exc)
    return problem(request, 500, "internal_error", "unexpected server error")


# -------------------------------------------------------------------- middleware


class RequestContext:
    """Assigns a request id (or keeps the caller's) and writes one JSON access log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if 0 < len(incoming) <= 128 and incoming.isprintable() else None
        request_id = request_id or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status,
                        "ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
            )


def create_app(db_path: str, clock: Clock | None = None) -> Starlette:
    db = Database(db_path)
    migrate(db)
    api = LedgerAPI(db, clock or SystemClock())

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        db.close()

    app = Starlette(
        routes=api.routes(),
        exception_handlers={
            LedgerError: ledger_error,
            HTTPException: http_error,
            DatabaseBusy: busy_error,
            sqlite3.IntegrityError: integrity_error,
            Exception: unexpected_error,
        },
        lifespan=lifespan,
    )
    app.add_middleware(RequestContext)
    app.state.db = db
    app.state.api = api
    return app
