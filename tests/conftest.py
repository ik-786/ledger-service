from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from ledger import create_app
from ledger.clock import FakeClock


class Api:
    """Thin helpers over the test client so tests read like API usage."""

    def __init__(self, client: TestClient, clock: FakeClock, db_path: Path) -> None:
        self.client = client
        self.clock = clock
        self.db_path = db_path
        self._funding: dict[str, str] = {}

    def post(self, path: str, body: dict[str, Any] | None = None, key: str | None = None) -> Any:
        headers = {"Idempotency-Key": key} if key else {}
        return self.client.post(path, json=body if body is not None else {}, headers=headers)

    def get(self, path: str) -> Any:
        return self.client.get(path)

    def account(
        self,
        currency: str = "USD",
        *,
        funded: int = 0,
        allow_negative: bool = False,
        name: str = "test",
    ) -> str:
        r = self.post(
            "/v1/accounts", {"name": name, "currency": currency, "allow_negative": allow_negative}
        )
        assert r.status_code == 201, r.text
        account_id: str = r.json()["id"]
        if funded:
            self.fund(account_id, funded, currency)
        return account_id

    def funding(self, currency: str = "USD") -> str:
        if currency not in self._funding:
            self._funding[currency] = self.account(currency, allow_negative=True, name="funding")
        return self._funding[currency]

    def fund(self, account_id: str, amount: int, currency: str = "USD") -> None:
        r = self.transfer(self.funding(currency), account_id, amount)
        assert r.status_code == 201, r.text

    def transfer(
        self, source: str, destination: str, amount: int, key: str | None = None, **extra: Any
    ) -> Any:
        body = {"from_account_id": source, "to_account_id": destination, "amount": amount, **extra}
        return self.post("/v1/transfers", body, key)

    def hold(self, source: str, destination: str, amount: int, seconds: int | None = None) -> Any:
        body: dict[str, Any] = {
            "account_id": source,
            "destination_account_id": destination,
            "amount": amount,
        }
        if seconds is not None:
            body["expires_in_seconds"] = seconds
        return self.post("/v1/holds", body)

    def balance(self, account_id: str) -> dict[str, int]:
        data = self.get(f"/v1/accounts/{account_id}").json()
        return {k: data[k] for k in ("balance", "held", "available")}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def api(db_path: Path, clock: FakeClock) -> Iterator[Api]:
    with TestClient(create_app(str(db_path), clock=clock)) as client:
        yield Api(client, clock, db_path)


def error_code(response: Any) -> str:
    assert response.headers["content-type"] == "application/problem+json", response.text
    code: str = response.json()["code"]
    return code
