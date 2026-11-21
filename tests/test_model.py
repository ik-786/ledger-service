"""Model-based test: random operation sequences against a tiny reference model.

The model is a few dicts and the rules written out in plain Python. Each step
sends one random request (valid or not) to the real API, predicts the outcome
with the model, and compares the status, the error code and every account's
balance, held and available amounts. At the end of each sequence the audit in
``ledger.check`` must find nothing.

Set LEDGER_MODEL_SEEDS to run more sequences, e.g. LEDGER_MODEL_SEEDS=500.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from ledger import Database, check, create_app
from ledger.clock import FakeClock

SEEDS = int(os.environ.get("LEDGER_MODEL_SEEDS", "30"))
STEPS = 120
GHOST = "acc_does_not_exist"


@dataclass
class Account:
    id: str
    currency: str
    negative_ok: bool
    balance: int = 0


@dataclass
class Hold:
    source: str
    destination: str
    amount: int
    expires_at: int
    status: str = "active"


@dataclass
class Txn:
    kind: str
    legs: list[tuple[str, int]]
    reversed: bool = False


Outcome = tuple[int, str | None]  # (HTTP status, error code or None)


@dataclass
class Model:
    clock: FakeClock
    accounts: dict[str, Account] = field(default_factory=dict)
    holds: dict[str, Hold] = field(default_factory=dict)
    txns: dict[str, Txn] = field(default_factory=dict)

    def held(self, account_id: str, exclude: str | None = None) -> int:
        now = self.clock.now_ms()
        return sum(
            h.amount
            for hid, h in self.holds.items()
            if h.source == account_id
            and h.status == "active"
            and h.expires_at > now
            and hid != exclude
        )

    def status(self, hold: Hold) -> str:
        if hold.status == "active" and hold.expires_at <= self.clock.now_ms():
            return "expired"
        return hold.status

    def predict_post(
        self, legs: list[tuple[str, int]], release: str | None = None
    ) -> Outcome | None:
        ids = [a for a, _ in legs]
        if len(set(ids)) != len(ids):
            return 422, "duplicate_account"
        if sum(x for _, x in legs) != 0:
            return 422, "unbalanced_transaction"
        for a in ids:
            if a not in self.accounts:
                return 404, "account_not_found"
        if len({self.accounts[a].currency for a in ids}) > 1:
            return 422, "currency_mismatch"
        for a, amount in legs:
            acct = self.accounts[a]
            short = acct.balance - self.held(a, release) + amount < 0
            if amount < 0 and not acct.negative_ok and short:
                return 422, "insufficient_funds"
        return None

    def apply(self, txn_id: str, kind: str, legs: list[tuple[str, int]]) -> None:
        for a, amount in legs:
            self.accounts[a].balance += amount
        self.txns[txn_id] = Txn(kind, legs)


class Runner:
    def __init__(self, seed: int, tmp: Path) -> None:
        self.rng = random.Random(seed)
        self.clock = FakeClock()
        self.db_path = tmp / f"model-{seed}.db"
        self.client = TestClient(create_app(str(self.db_path), clock=self.clock))
        self.model = Model(self.clock)
        self.log: list[str] = []

    def call(self, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(path, json=body)

    def expect(self, what: str, response: Any, predicted: Outcome | None, ok: int) -> bool:
        actual = (
            response.status_code,
            None if response.status_code < 300 else response.json()["code"],
        )
        wanted = predicted or (ok, None)
        self.log.append(f"{what} -> {actual}")
        assert actual == wanted, "\n".join(self.log[-15:]) + f"\npredicted {wanted}"
        return predicted is None

    def pick_account(self) -> str:
        if self.rng.random() < 0.04:
            return GHOST
        return self.rng.choice(list(self.model.accounts))

    # ---------------------------------------------------------------- operations

    def open_accounts(self) -> None:
        for currency, negative_ok, count in (
            ("USD", True, 1),
            ("EUR", True, 1),
            ("USD", False, 4),
            ("EUR", False, 1),
        ):
            for _ in range(count):
                r = self.call(
                    "/v1/accounts",
                    {"name": "m", "currency": currency, "allow_negative": negative_ok},
                )
                acct = r.json()
                self.model.accounts[acct["id"]] = Account(acct["id"], currency, negative_ok)
        funders = {a.currency: a.id for a in self.model.accounts.values() if a.negative_ok}
        for acct in list(self.model.accounts.values()):
            if not acct.negative_ok:
                amount = self.rng.randint(500, 3_000)
                r = self.call(
                    "/v1/transfers",
                    {
                        "from_account_id": funders[acct.currency],
                        "to_account_id": acct.id,
                        "amount": amount,
                    },
                )
                self.model.apply(
                    r.json()["id"],
                    "transfer",
                    [(funders[acct.currency], -amount), (acct.id, amount)],
                )

    def transfer(self) -> None:
        a, b = self.pick_account(), self.pick_account()
        amount = self.rng.randint(1, 1_500)
        legs = [(a, -amount), (b, amount)]
        predicted = (422, "same_account") if a == b else self.model.predict_post(legs)
        r = self.call("/v1/transfers", {"from_account_id": a, "to_account_id": b, "amount": amount})
        if self.expect(f"transfer {a} {b} {amount}", r, predicted, 201):
            self.model.apply(r.json()["id"], "transfer", legs)

    def journal(self) -> None:
        a, b, c = (self.pick_account() for _ in range(3))
        x = self.rng.randint(2, 1_200)
        y = self.rng.randint(1, x - 1)
        legs = [(a, -x), (b, y), (c, x - y)]
        if self.rng.random() < 0.1:
            legs[2] = (c, x - y + self.rng.choice((-1, 1)))
            if legs[2][1] == 0:
                legs[2] = (c, 1)
        r = self.call(
            "/v1/transactions", {"legs": [{"account_id": i, "amount": v} for i, v in legs]}
        )
        if self.expect(f"journal {legs}", r, self.model.predict_post(legs), 201):
            self.model.apply(r.json()["id"], "journal", legs)

    def hold(self) -> None:
        a, b = self.pick_account(), self.pick_account()
        amount = self.rng.randint(1, 1_200)
        seconds = self.rng.randint(30, 900)
        m = self.model
        predicted: Outcome | None = None
        if a == b:
            predicted = (422, "same_account")
        elif a not in m.accounts or b not in m.accounts:
            predicted = (404, "account_not_found")
        elif m.accounts[a].currency != m.accounts[b].currency:
            predicted = (422, "currency_mismatch")
        elif not m.accounts[a].negative_ok and m.accounts[a].balance - m.held(a) < amount:
            predicted = (422, "insufficient_funds")
        body = {
            "account_id": a,
            "destination_account_id": b,
            "amount": amount,
            "expires_in_seconds": seconds,
        }
        r = self.call("/v1/holds", body)
        if self.expect(f"hold {a} {b} {amount} {seconds}s", r, predicted, 201):
            m.holds[r.json()["id"]] = Hold(a, b, amount, self.clock.now_ms() + seconds * 1000)

    def settle_hold(self) -> None:
        m = self.model
        hold_id = (
            self.rng.choice(list(m.holds)) if m.holds and self.rng.random() > 0.05 else "hold_ghost"
        )
        hold = m.holds.get(hold_id)
        capture = self.rng.random() < 0.7
        amount = (
            None
            if hold is None or self.rng.random() < 0.3
            else self.rng.randint(1, int(hold.amount * 1.2) + 1)
        )
        predicted: Outcome | None = None
        if hold is None:
            predicted = (404, "hold_not_found")
        elif m.status(hold) == "expired":
            predicted = (409, "hold_expired")
        elif hold.status != "active":
            predicted = (409, "hold_not_active")
        elif capture and amount is not None and amount > hold.amount:
            predicted = (422, "capture_exceeds_hold")
        if capture:
            body = {} if amount is None else {"amount": amount}
            r = self.call(f"/v1/holds/{hold_id}/capture", body)
            if self.expect(f"capture {hold_id} {amount}", r, predicted, 200):
                assert hold is not None
                taken = hold.amount if amount is None else amount
                m.apply(
                    r.json()["transaction_id"],
                    "capture",
                    [(hold.source, -taken), (hold.destination, taken)],
                )
                hold.status = "captured"
        else:
            r = self.call(f"/v1/holds/{hold_id}/void", {})
            if self.expect(f"void {hold_id}", r, predicted, 200):
                assert hold is not None
                hold.status = "voided"

    def reverse(self) -> None:
        m = self.model
        if not m.txns:
            return
        txn_id = self.rng.choice(list(m.txns))
        txn = m.txns[txn_id]
        legs = [(a, -x) for a, x in txn.legs]
        predicted: Outcome | None
        if txn.kind == "reversal":
            predicted = (422, "cannot_reverse_reversal")
        elif txn.reversed:
            predicted = (409, "already_reversed")
        else:
            predicted = m.predict_post(legs)
        r = self.call(f"/v1/transactions/{txn_id}/reverse", {})
        if self.expect(f"reverse {txn_id}", r, predicted, 201):
            m.apply(r.json()["id"], "reversal", legs)
            txn.reversed = True

    def wait(self) -> None:
        seconds = self.rng.choice((1, 30, 120, 600))
        self.clock.advance(seconds)
        self.log.append(f"wait {seconds}s")

    def compare_balances(self) -> None:
        listed = {a["id"]: a for a in self.client.get("/v1/accounts?limit=100").json()["data"]}
        for acct in self.model.accounts.values():
            held = self.model.held(acct.id)
            actual = listed[acct.id]
            expected = {"balance": acct.balance, "held": held, "available": acct.balance - held}
            got = {k: actual[k] for k in expected}
            assert got == expected, "\n".join(self.log[-15:]) + f"\n{acct.id}: {got} != {expected}"

    def run(self, steps: int) -> int:
        self.open_accounts()
        ops = (
            [self.transfer] * 5
            + [self.journal] * 2
            + [self.hold] * 3
            + [self.settle_hold] * 3
            + [self.reverse] * 2
            + [self.wait]
        )
        for _ in range(steps):
            self.rng.choice(ops)()
            self.compare_balances()
        db = Database(str(self.db_path))
        try:
            assert check(db) == []
        finally:
            db.close()
        return len(self.log)


@pytest.mark.parametrize("seed", range(SEEDS))
def test_random_operations_match_the_model(seed: int, tmp_path: Path) -> None:
    Runner(seed, tmp_path).run(STEPS)
