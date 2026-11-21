"""Ledger rules. Every method takes a connection that is already inside a transaction.

Amounts are signed integers in minor units. A positive entry increases an
account's balance, a negative one decreases it, and the entries of every
transaction sum to zero, so money is only ever moved, never created.
New money comes from an account created with ``allow_negative`` (for example
a funding or settlement account), whose balance goes negative as it pays out.

Available funds are the posted balance minus active, unexpired holds. Every
debit from a normal account, including a reversal, must fit in available funds.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import sqlite3
from typing import Any

from .clock import Clock, iso
from .errors import conflict, invalid, not_found, rule
from .schemas import MAX_BALANCE

Row = sqlite3.Row
Legs = list[tuple[str, int]]

_ACCOUNT_COLUMNS = """
    a.rowid AS position, a.id, a.name, a.currency, a.allow_negative, a.balance, a.created_at,
    (SELECT COALESCE(SUM(h.amount), 0) FROM holds h
      WHERE h.account_id = a.id AND h.status = 'active' AND h.expires_at > :now) AS held
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def encode_cursor(scope: str, position: int) -> str:
    raw = json.dumps({"s": scope, "p": position}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, scope: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        position = data["p"]
        owner = data["s"]
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise invalid("cursor is malformed") from None
    if owner != scope or not isinstance(position, int) or isinstance(position, bool):
        raise invalid("cursor does not belong to this listing")
    return position


class Ledger:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    # ------------------------------------------------------------------ accounts

    def create_account(
        self, conn: sqlite3.Connection, name: str, currency: str, allow_negative: bool
    ) -> dict[str, Any]:
        account_id = new_id("acc")
        conn.execute(
            "INSERT INTO accounts (id, name, currency, allow_negative, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (account_id, name, currency, int(allow_negative), self.clock.now_ms()),
        )
        return self.get_account(conn, account_id)

    def get_account(self, conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts a WHERE a.id = :id",
            {"id": account_id, "now": self.clock.now_ms()},
        ).fetchone()
        if row is None:
            raise not_found("account", account_id)
        return _account_json(row)

    def list_accounts(
        self, conn: sqlite3.Connection, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        after = decode_cursor(cursor, "accounts") if cursor else 0
        rows = conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts a WHERE a.rowid > :after"
            " ORDER BY a.rowid LIMIT :limit",
            {"after": after, "limit": limit + 1, "now": self.clock.now_ms()},
        ).fetchall()
        return _page(rows, limit, "accounts", _account_json)

    def list_entries(
        self, conn: sqlite3.Connection, account_id: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        """Entries of one account, newest first, each with the balance right after it."""
        self._account_row(conn, account_id)
        scope = f"entries:{account_id}"
        before = decode_cursor(cursor, scope) if cursor else None
        rows = conn.execute(
            "SELECT e.id AS position, e.id, e.transaction_id, t.kind, e.amount, e.balance_after,"
            " e.created_at FROM entries e JOIN transactions t ON t.id = e.transaction_id"
            " WHERE e.account_id = :account AND (:before IS NULL OR e.id < :before)"
            " ORDER BY e.id DESC LIMIT :limit",
            {"account": account_id, "before": before, "limit": limit + 1},
        ).fetchall()
        return _page(rows, limit, scope, _entry_json)

    def _account_row(self, conn: sqlite3.Connection, account_id: str) -> Row:
        row: Row | None = conn.execute(
            "SELECT id, currency, allow_negative, balance FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise not_found("account", account_id)
        return row

    def _held(self, conn: sqlite3.Connection, account_id: str, exclude: str | None = None) -> int:
        held: int = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM holds WHERE account_id = ? AND status = 'active'"
            " AND expires_at > ? AND id IS NOT ?",
            (account_id, self.clock.now_ms(), exclude),
        ).fetchone()[0]
        return held

    # -------------------------------------------------------------- transactions

    def transfer(
        self,
        conn: sqlite3.Connection,
        source: str,
        destination: str,
        amount: int,
        description: str | None,
    ) -> dict[str, Any]:
        if source == destination:
            raise rule("same_account", "source and destination must be different accounts")
        txn_id = self._post(
            conn, "transfer", [(source, -amount), (destination, amount)], description
        )
        return self.get_transaction(conn, txn_id)

    def post_journal(
        self, conn: sqlite3.Connection, legs: Legs, description: str | None
    ) -> dict[str, Any]:
        txn_id = self._post(conn, "journal", legs, description)
        return self.get_transaction(conn, txn_id)

    def reverse(
        self, conn: sqlite3.Connection, txn_id: str, description: str | None
    ) -> dict[str, Any]:
        original = self.get_transaction(conn, txn_id)
        if original["kind"] == "reversal":
            raise rule("cannot_reverse_reversal", "a reversal cannot itself be reversed")
        if original["reversed_by"] is not None:
            raise conflict(
                "already_reversed",
                f"transaction {txn_id!r} was already reversed",
                reversed_by=original["reversed_by"],
            )
        legs = [(e["account_id"], -e["amount"]) for e in original["entries"]]
        new_txn = self._post(
            conn, "reversal", legs, description or f"reversal of {txn_id}", reverses_id=txn_id
        )
        return self.get_transaction(conn, new_txn)

    def get_transaction(self, conn: sqlite3.Connection, txn_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if row is None:
            raise not_found("transaction", txn_id)
        entries = conn.execute(
            "SELECT id, account_id, amount, balance_after FROM entries"
            " WHERE transaction_id = ? ORDER BY id",
            (txn_id,),
        ).fetchall()
        reversed_by = conn.execute(
            "SELECT id FROM transactions WHERE reverses_id = ?", (txn_id,)
        ).fetchone()
        return {
            "id": row["id"],
            "kind": row["kind"],
            "currency": row["currency"],
            "description": row["description"],
            "hold_id": row["hold_id"],
            "reverses_id": row["reverses_id"],
            "reversed_by": reversed_by["id"] if reversed_by else None,
            "created_at": iso(row["created_at"]),
            "entries": [
                {
                    "id": e["id"],
                    "account_id": e["account_id"],
                    "amount": e["amount"],
                    "balance_after": e["balance_after"],
                }
                for e in entries
            ],
        }

    def _post(
        self,
        conn: sqlite3.Connection,
        kind: str,
        legs: Legs,
        description: str | None,
        *,
        reverses_id: str | None = None,
        hold_id: str | None = None,
        release_hold: str | None = None,
    ) -> str:
        """Validate and write one balanced transaction. Checks run in a fixed order,
        so a request that breaks several rules always gets the same error."""
        account_ids = [account_id for account_id, _ in legs]
        if len(set(account_ids)) != len(account_ids):
            raise rule("duplicate_account", "each account may appear in only one leg")
        imbalance = sum(amount for _, amount in legs)
        if imbalance != 0:
            raise rule(
                "unbalanced_transaction",
                f"legs must sum to zero; they sum to {imbalance}",
                imbalance=imbalance,
            )

        accounts = {account_id: self._account_row(conn, account_id) for account_id in account_ids}
        currencies = sorted({row["currency"] for row in accounts.values()})
        if len(currencies) > 1:
            raise rule(
                "currency_mismatch",
                f"all accounts in a transaction must share a currency; got {', '.join(currencies)}",
            )

        for account_id, amount in legs:
            row = accounts[account_id]
            if amount < 0 and not row["allow_negative"]:
                available = row["balance"] - self._held(conn, account_id, exclude=release_hold)
                if available + amount < 0:
                    raise rule(
                        "insufficient_funds",
                        f"account {account_id!r} has {available} available and needs {-amount}",
                        account_id=account_id,
                        available=available,
                        required=-amount,
                    )
            if abs(row["balance"] + amount) > MAX_BALANCE:
                raise rule(
                    "balance_limit_exceeded",
                    f"account {account_id!r} would pass the balance limit of {MAX_BALANCE}",
                    account_id=account_id,
                )

        now = self.clock.now_ms()
        txn_id = new_id("txn")
        conn.execute(
            "INSERT INTO transactions (id, kind, currency, description, reverses_id, hold_id,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (txn_id, kind, currencies[0], description, reverses_id, hold_id, now),
        )
        for account_id, amount in legs:
            balance = conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE id = ? RETURNING balance",
                (amount, account_id),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO entries"
                " (transaction_id, account_id, amount, balance_after, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (txn_id, account_id, amount, balance, now),
            )
        return txn_id

    # --------------------------------------------------------------------- holds

    def create_hold(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        destination_id: str,
        amount: int,
        expires_in_seconds: int,
    ) -> dict[str, Any]:
        """Reserve funds now, to be captured into ``destination_id`` or released later."""
        if account_id == destination_id:
            raise rule("same_account", "source and destination must be different accounts")
        source = self._account_row(conn, account_id)
        destination = self._account_row(conn, destination_id)
        if source["currency"] != destination["currency"]:
            raise rule(
                "currency_mismatch",
                "all accounts in a transaction must share a currency;"
                f" got {source['currency']}, {destination['currency']}",
            )
        if not source["allow_negative"]:
            available = source["balance"] - self._held(conn, account_id)
            if available < amount:
                raise rule(
                    "insufficient_funds",
                    f"account {account_id!r} has {available} available and needs {amount}",
                    account_id=account_id,
                    available=available,
                    required=amount,
                )
        now = self.clock.now_ms()
        hold_id = new_id("hold")
        conn.execute(
            "INSERT INTO holds (id, account_id, destination_id, amount, status, expires_at,"
            " created_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (hold_id, account_id, destination_id, amount, now + expires_in_seconds * 1000, now),
        )
        return self.get_hold(conn, hold_id)

    def get_hold(self, conn: sqlite3.Connection, hold_id: str) -> dict[str, Any]:
        row = self._hold_row(conn, hold_id)
        txn = conn.execute("SELECT id FROM transactions WHERE hold_id = ?", (hold_id,)).fetchone()
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "destination_account_id": row["destination_id"],
            "amount": row["amount"],
            "status": self._status(row),
            "captured_amount": row["captured_amount"],
            "transaction_id": txn["id"] if txn else None,
            "expires_at": iso(row["expires_at"]),
            "created_at": iso(row["created_at"]),
            "closed_at": iso(row["closed_at"]),
        }

    def capture_hold(
        self, conn: sqlite3.Connection, hold_id: str, amount: int | None
    ) -> dict[str, Any]:
        """Move up to the held amount to the destination. Whatever is not captured is released."""
        row = self._hold_row(conn, hold_id)
        self._require_active(row)
        capture = row["amount"] if amount is None else amount
        if capture > row["amount"]:
            raise rule(
                "capture_exceeds_hold",
                f"cannot capture {capture} from a hold of {row['amount']}",
                held=row["amount"],
            )
        self._post(
            conn,
            "capture",
            [(row["account_id"], -capture), (row["destination_id"], capture)],
            f"capture of {hold_id}",
            hold_id=hold_id,
            release_hold=hold_id,
        )
        conn.execute(
            "UPDATE holds SET status = 'captured', captured_amount = ?, closed_at = ? WHERE id = ?",
            (capture, self.clock.now_ms(), hold_id),
        )
        return self.get_hold(conn, hold_id)

    def void_hold(self, conn: sqlite3.Connection, hold_id: str) -> dict[str, Any]:
        row = self._hold_row(conn, hold_id)
        self._require_active(row)
        conn.execute(
            "UPDATE holds SET status = 'voided', closed_at = ? WHERE id = ?",
            (self.clock.now_ms(), hold_id),
        )
        return self.get_hold(conn, hold_id)

    def _hold_row(self, conn: sqlite3.Connection, hold_id: str) -> Row:
        row: Row | None = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if row is None:
            raise not_found("hold", hold_id)
        return row

    def _status(self, row: Row) -> str:
        """Expiry is evaluated on read, so no background job is needed to release funds."""
        if row["status"] == "active" and row["expires_at"] <= self.clock.now_ms():
            return "expired"
        status: str = row["status"]
        return status

    def _require_active(self, row: Row) -> None:
        status = self._status(row)
        if status == "expired":
            raise conflict("hold_expired", f"hold {row['id']!r} has expired", hold_status=status)
        if status != "active":
            raise conflict("hold_not_active", f"hold {row['id']!r} is {status}", hold_status=status)


def _account_json(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "currency": row["currency"],
        "allow_negative": bool(row["allow_negative"]),
        "balance": row["balance"],
        "held": row["held"],
        "available": row["balance"] - row["held"],
        "created_at": iso(row["created_at"]),
    }


def _entry_json(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "transaction_id": row["transaction_id"],
        "kind": row["kind"],
        "amount": row["amount"],
        "balance_after": row["balance_after"],
        "created_at": iso(row["created_at"]),
    }


def _page(rows: list[Row], limit: int, scope: str, render: Any) -> dict[str, Any]:
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "data": [render(row) for row in rows],
        "next_cursor": encode_cursor(scope, rows[-1]["position"]) if more else None,
    }
