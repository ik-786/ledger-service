"""Many writers at once, on a real SQLite file, through separate connections.

The funds check and the debit are two statements, so the ledger is only correct
if no other writer can get between them. Two things keep writers apart. Inside
one process, writers queue on a lock in ``Database.write``. Across processes,
``BEGIN IMMEDIATE`` takes SQLite's write lock before any balance is read.

The first two tests write through four ``Database`` objects on one file, which
stand in for four server processes, so they rely on ``BEGIN IMMEDIATE``: with a
deferred ``BEGIN`` they fail. The HTTP test covers the single-process path.
"""

from __future__ import annotations

import random
import socket
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import uvicorn

from ledger import Database, Ledger, check, create_app, migrate
from ledger.clock import FakeClock
from ledger.errors import LedgerError

PROCESSES = 4


def _ledger(path: Path) -> tuple[list[Database], Ledger]:
    """One Database per simulated process: separate connections, separate in-process locks."""
    dbs = [Database(str(path)) for _ in range(PROCESSES)]
    migrate(dbs[0])
    return dbs, Ledger(FakeClock())


def _run(threads: int, target, *args) -> None:  # type: ignore[no-untyped-def]
    """Start every worker at the same moment; re-raise the first error any of them hit."""
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            target(i, *args)
        except BaseException as exc:
            errors.append(exc)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    if errors:
        raise errors[0]


def test_no_overdraft_when_many_spend_at_once(tmp_path: Path) -> None:
    dbs, ledger = _ledger(tmp_path / "l.db")
    db = dbs[0]
    with db.write() as conn:
        source = ledger.create_account(conn, "funding", "USD", True)["id"]
        wallet = ledger.create_account(conn, "wallet", "USD", False)["id"]
        shop = ledger.create_account(conn, "shop", "USD", False)["id"]
        ledger.transfer(conn, source, wallet, 200, None)

    outcomes: Counter[str] = Counter()
    lock = threading.Lock()

    def spend(i: int) -> None:
        try:
            with dbs[i % PROCESSES].write() as conn:
                ledger.transfer(conn, wallet, shop, 10, f"order {i}")
            result = "ok"
        except LedgerError as exc:
            result = exc.code
        with lock:
            outcomes[result] += 1

    _run(40, spend)
    assert outcomes == {"ok": 20, "insufficient_funds": 20}
    with db.read() as conn:
        assert ledger.get_account(conn, wallet)["balance"] == 0
        assert ledger.get_account(conn, shop)["balance"] == 200
    assert check(db) == []
    for d in dbs:
        d.close()


def test_holds_and_transfers_race_without_breaking_invariants(tmp_path: Path) -> None:
    dbs, ledger = _ledger(tmp_path / "l.db")
    db = dbs[0]
    with db.write() as conn:
        source = ledger.create_account(conn, "funding", "USD", True)["id"]
        wallets = [ledger.create_account(conn, f"w{i}", "USD", False)["id"] for i in range(6)]
        for w in wallets:
            ledger.transfer(conn, source, w, 1_000, None)

    def chaos(i: int) -> None:
        rng = random.Random(i)
        holds: list[str] = []
        for _ in range(40):
            a, b = rng.sample(wallets, 2)
            amount = rng.randint(1, 400)
            try:
                with dbs[i % PROCESSES].write() as conn:
                    op = rng.random()
                    if op < 0.5:
                        ledger.transfer(conn, a, b, amount, None)
                    elif op < 0.75:
                        holds.append(ledger.create_hold(conn, a, b, amount, 3600)["id"])
                    elif holds:
                        hold = holds.pop(rng.randrange(len(holds)))
                        if op < 0.9:
                            ledger.capture_hold(
                                conn, hold, rng.randint(1, amount) if rng.random() < 0.5 else None
                            )
                        else:
                            ledger.void_hold(conn, hold)
            except LedgerError as exc:
                assert exc.code in {"insufficient_funds", "capture_exceeds_hold"}, exc.code

    _run(12, chaos)
    assert check(db) == []
    with db.read() as conn:
        accounts = [ledger.get_account(conn, w) for w in wallets]
    assert sum(a["balance"] for a in accounts) == 6_000
    assert all(a["available"] >= 0 for a in accounts)
    for d in dbs:
        d.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_real_http_server_under_parallel_clients(tmp_path: Path) -> None:
    """The full stack: uvicorn, the thread pool and one SQLite file."""
    db_path = tmp_path / "l.db"
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(str(db_path)), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base) as c:
            source = c.post(
                "/v1/accounts", json={"name": "f", "currency": "USD", "allow_negative": True}
            ).json()["id"]
            wallet = c.post("/v1/accounts", json={"name": "w", "currency": "USD"}).json()["id"]
            shop = c.post("/v1/accounts", json={"name": "s", "currency": "USD"}).json()["id"]
            c.post(
                "/v1/transfers",
                json={"from_account_id": source, "to_account_id": wallet, "amount": 500},
            )

        statuses: Counter[int] = Counter()
        lock = threading.Lock()

        def client(i: int) -> None:
            with httpx.Client(base_url=base) as c:
                for j in range(5):
                    r = c.post(
                        "/v1/transfers",
                        json={"from_account_id": wallet, "to_account_id": shop, "amount": 10},
                        headers={"Idempotency-Key": f"order-{i}-{j % 3}"},  # retries included
                    )
                    with lock:
                        statuses[r.status_code] += 1

        _run(16, client)
        # 16 clients x 3 distinct keys = 48 transfers of 10; the other 32 requests are retries.
        assert statuses == {201: 80}
        with httpx.Client(base_url=base) as c:
            assert c.get(f"/v1/accounts/{wallet}").json()["balance"] == 500 - 480
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    db = Database(str(db_path))
    assert check(db) == []
    db.close()
