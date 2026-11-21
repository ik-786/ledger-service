"""Independent audit of a ledger database.

Recomputes everything the service caches or promises from the raw tables, so a
bug in the write path (or a manual edit of the file) shows up here instead of
in somebody's balance. Run it with ``python -m ledger check --db ledger.db``.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .db import Database


def check(db: Database) -> list[str]:
    with db.read() as conn:
        return _check(conn)


def _check(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    accounts = {row["id"]: row for row in conn.execute("SELECT * FROM accounts")}
    transactions = {row["id"]: row for row in conn.execute("SELECT * FROM transactions")}

    legs: dict[str, list[sqlite3.Row]] = defaultdict(list)
    running: dict[str, int] = defaultdict(int)
    for entry in conn.execute("SELECT * FROM entries ORDER BY id"):
        legs[entry["transaction_id"]].append(entry)
        account = accounts.get(entry["account_id"])
        if account is None:
            problems.append(f"entry {entry['id']}: unknown account {entry['account_id']}")
            continue
        running[entry["account_id"]] += entry["amount"]
        if entry["balance_after"] != running[entry["account_id"]]:
            problems.append(
                f"entry {entry['id']}: balance_after {entry['balance_after']} but the entries"
                f" of {entry['account_id']} sum to {running[entry['account_id']]}"
            )
        if not account["allow_negative"] and running[entry["account_id"]] < 0:
            problems.append(
                f"entry {entry['id']}: account {entry['account_id']} went negative"
                f" ({running[entry['account_id']]})"
            )

    # 1. Every transaction balances, in a single currency.
    for txn_id, txn in transactions.items():
        entries = legs.get(txn_id, [])
        if len(entries) < 2:
            problems.append(f"transaction {txn_id}: has {len(entries)} entries, needs at least 2")
        total = sum(e["amount"] for e in entries)
        if total != 0:
            problems.append(f"transaction {txn_id}: entries sum to {total}, not 0")
        for e in entries:
            account = accounts.get(e["account_id"])
            if account is not None and account["currency"] != txn["currency"]:
                problems.append(
                    f"transaction {txn_id}: {txn['currency']} transaction posts to"
                    f" {account['currency']} account {e['account_id']}"
                )
    for txn_id in legs.keys() - transactions.keys():
        problems.append(f"entries reference missing transaction {txn_id}")

    # 2. Cached balances equal the sum of entries.
    for account_id, account in accounts.items():
        if account["balance"] != running.get(account_id, 0):
            problems.append(
                f"account {account_id}: cached balance {account['balance']} but entries sum"
                f" to {running.get(account_id, 0)}"
            )

    # 3. A reversal is the exact mirror of what it reverses.
    for txn_id, txn in transactions.items():
        if txn["reverses_id"] is None:
            continue
        original = transactions.get(txn["reverses_id"])
        if original is None:
            problems.append(f"reversal {txn_id}: reverses missing transaction {txn['reverses_id']}")
            continue
        if txn["kind"] != "reversal" or original["kind"] == "reversal":
            problems.append(f"reversal {txn_id}: kinds {txn['kind']} -> {original['kind']}")
        mine = sorted((e["account_id"], e["amount"]) for e in legs.get(txn_id, []))
        mirror = sorted((e["account_id"], -e["amount"]) for e in legs.get(original["id"], []))
        if mine != mirror:
            problems.append(f"reversal {txn_id}: does not mirror {original['id']}")

    # 4. Captured holds have exactly one capture moving the captured amount; others none.
    captures = {t["hold_id"]: t for t in transactions.values() if t["hold_id"] is not None}
    for hold in conn.execute("SELECT * FROM holds"):
        txn = captures.pop(hold["id"], None)
        if hold["status"] != "captured":
            if txn is not None:
                problems.append(f"hold {hold['id']}: {hold['status']} but captured by {txn['id']}")
            continue
        if txn is None:
            problems.append(f"hold {hold['id']}: captured but no capture transaction")
            continue
        expected = sorted(
            [
                (hold["account_id"], -hold["captured_amount"]),
                (hold["destination_id"], hold["captured_amount"]),
            ]
        )
        actual = sorted((e["account_id"], e["amount"]) for e in legs.get(txn["id"], []))
        if txn["kind"] != "capture" or actual != expected:
            problems.append(f"hold {hold['id']}: capture {txn['id']} does not match the hold")
        if hold["captured_amount"] > hold["amount"]:
            problems.append(
                f"hold {hold['id']}: captured {hold['captured_amount']} of {hold['amount']}"
            )
    for hold_id, txn in captures.items():
        problems.append(f"transaction {txn['id']}: captures missing hold {hold_id}")

    return problems
