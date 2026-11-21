"""The audit must pass on real data and catch each kind of corruption."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ledger import Database, check, migrate
from ledger.__main__ import main
from ledger.db import _statements

from .conftest import Api


@pytest.fixture
def populated(api: Api) -> dict[str, str]:
    alice = api.account(funded=1_000)
    bob = api.account()
    t = api.transfer(alice, bob, 300).json()
    hold = api.hold(alice, bob, 200).json()
    api.post(f"/v1/holds/{hold['id']}/capture", {"amount": 150})
    reversal = api.post(f"/v1/transactions/{t['id']}/reverse").json()
    return {
        "alice": alice,
        "bob": bob,
        "txn": t["id"],
        "hold": hold["id"],
        "reversal": reversal["id"],
    }


def tamper(path: Path, *statements: str) -> list[str]:
    """Edit the file behind the service's back, the way a bad migration or a person might."""
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER entries_are_append_only_u")
    conn.execute("DROP TRIGGER transactions_are_append_only_u")
    conn.execute("PRAGMA ignore_check_constraints = ON")
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()
    db = Database(str(path))
    try:
        return check(db)
    finally:
        db.close()


def test_clean_ledger_passes(api: Api, populated: dict[str, str]) -> None:
    db = Database(str(api.db_path))
    assert check(db) == []
    db.close()


@pytest.mark.parametrize(
    "edit, message",
    [
        ("UPDATE accounts SET balance = balance + 1 WHERE id = '{alice}'", "cached balance"),
        (
            "UPDATE entries SET amount = amount + 5 WHERE id = (SELECT MIN(id) FROM entries WHERE account_id = '{bob}')",
            "sum to 5",
        ),
        (
            "UPDATE entries SET balance_after = 7 WHERE id = (SELECT MAX(id) FROM entries)",
            "balance_after 7",
        ),
        ("UPDATE accounts SET currency = 'EUR' WHERE id = '{bob}'", "posts to EUR account"),
        ("UPDATE holds SET captured_amount = 151 WHERE id = '{hold}'", "does not match the hold"),
        ("UPDATE holds SET status = 'voided' WHERE id = '{hold}'", "voided but captured"),
        (
            "UPDATE transactions SET hold_id = NULL WHERE hold_id = '{hold}'",
            "captured but no capture",
        ),
        (
            "UPDATE transactions SET reverses_id = (SELECT id FROM transactions WHERE hold_id = '{hold}') WHERE id = '{reversal}'",
            "does not mirror",
        ),
    ],
)
def test_corruption_is_reported(
    api: Api, populated: dict[str, str], edit: str, message: str
) -> None:
    problems = tamper(api.db_path, edit.format(**populated))
    assert any(message in p for p in problems), problems


def test_overdraft_in_history_is_reported(api: Api, populated: dict[str, str]) -> None:
    # Shift money between two of alice's entries: the final balance still adds up,
    # but at one point in her history the account was below zero.
    first, second = (
        "(SELECT id FROM entries WHERE account_id = '{alice}' ORDER BY id LIMIT 1 OFFSET {n})".format(
            n=n, **populated
        )
        for n in (0, 1)
    )
    problems = tamper(
        api.db_path,
        f"UPDATE entries SET amount = amount - 2000, balance_after = balance_after - 2000 WHERE id = {first}",
        f"UPDATE entries SET amount = amount + 2000 WHERE id = {second}",
    )
    assert any("went negative" in p for p in problems), problems


def test_entries_and_transactions_are_append_only(api: Api, populated: dict[str, str]) -> None:
    conn = sqlite3.connect(api.db_path)
    for statement in (
        "UPDATE entries SET amount = 1",
        "DELETE FROM entries",
        "UPDATE transactions SET description = 'x'",
        "DELETE FROM transactions",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)
    conn.close()


def test_database_refuses_negative_balance_on_normal_accounts(
    api: Api, populated: dict[str, str]
) -> None:
    conn = sqlite3.connect(api.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(f"UPDATE accounts SET balance = -1 WHERE id = '{populated['alice']}'")
    conn.close()


def test_cli(
    api: Api, populated: dict[str, str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["check", "--db", str(api.db_path)]) == 0
    assert "consistent" in capsys.readouterr().out
    tamper(api.db_path, f"UPDATE accounts SET balance = 0 WHERE id = '{populated['alice']}'")
    assert main(["check", "--db", str(api.db_path)]) == 1
    assert main(["check", "--db", str(tmp_path / "missing.db")]) == 2
    fresh = tmp_path / "fresh.db"
    assert main(["migrate", "--db", str(fresh)]) == 0
    assert "0001_init, 0002_idempotency_keys" in capsys.readouterr().out
    assert main(["migrate", "--db", str(fresh)]) == 0
    assert "up to date" in capsys.readouterr().out


def test_migrations_are_recorded_once(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "m.db"))
    assert migrate(db) == ["0001_init", "0002_idempotency_keys"]
    assert migrate(db) == []
    with db.read() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
    assert names == ["0001_init", "0002_idempotency_keys"]
    db.close()


def test_statement_splitter_keeps_trigger_bodies_whole() -> None:
    script = "CREATE TABLE t (x);\nCREATE TRIGGER g BEFORE DELETE ON t\nBEGIN\n  SELECT 1;\nEND;\n-- trailing note\n"
    statements = list(_statements(script))
    assert len(statements) == 2
    assert statements[1].endswith("END;")
    with pytest.raises(ValueError):
        list(_statements("CREATE TABLE broken (x"))
