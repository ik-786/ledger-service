from __future__ import annotations

import threading
from pathlib import Path

from starlette.testclient import TestClient

from ledger import create_app
from ledger.clock import FakeClock

from .conftest import Api, error_code


def test_retry_with_same_key_replays_the_response(api: Api) -> None:
    alice = api.account(funded=1_000)
    bob = api.account()
    first = api.transfer(alice, bob, 100, key="pay-42")
    second = api.transfer(alice, bob, 100, key="pay-42")
    assert first.status_code == second.status_code == 201
    assert second.content == first.content
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert api.balance(alice)["balance"] == 900


def test_key_order_and_whitespace_do_not_matter(api: Api) -> None:
    alice = api.account(funded=1_000)
    bob = api.account()
    api.transfer(alice, bob, 100, key="k")
    body = f'{{ "amount": 100, "to_account_id": "{bob}",  "from_account_id": "{alice}" }}'
    r = api.client.post(
        "/v1/transfers",
        content=body,
        headers={"Idempotency-Key": "k", "Content-Type": "application/json"},
    )
    assert r.headers.get("Idempotent-Replayed") == "true"
    assert api.balance(alice)["balance"] == 900


def test_same_key_different_request_is_rejected(api: Api) -> None:
    alice = api.account(funded=1_000)
    bob = api.account()
    api.transfer(alice, bob, 100, key="k")
    r = api.transfer(alice, bob, 200, key="k")
    assert r.status_code == 422
    assert error_code(r) == "idempotency_key_reused"
    r = api.post("/v1/accounts", {"name": "x", "currency": "USD"}, key="k")
    assert error_code(r) == "idempotency_key_reused"
    assert api.balance(alice)["balance"] == 900


def test_failed_requests_are_not_stored(api: Api) -> None:
    """A refused request commits nothing, so the same key can be retried once it can succeed."""
    alice = api.account()
    bob = api.account()
    assert error_code(api.transfer(alice, bob, 100, key="k")) == "insufficient_funds"
    api.fund(alice, 100)
    r = api.transfer(alice, bob, 100, key="k")
    assert r.status_code == 201
    assert "Idempotent-Replayed" not in r.headers


def test_keys_expire_after_24_hours(api: Api) -> None:
    alice = api.account(funded=1_000)
    bob = api.account()
    api.transfer(alice, bob, 100, key="k")
    api.clock.advance(24 * 3600 - 1)
    assert api.transfer(alice, bob, 100, key="k").headers.get("Idempotent-Replayed") == "true"
    api.clock.advance(1)
    r = api.transfer(alice, bob, 100, key="k")
    assert "Idempotent-Replayed" not in r.headers
    assert api.balance(alice)["balance"] == 800


def test_bad_keys(api: Api) -> None:
    for key in ("x" * 256, "tab\tkey"):
        r = api.post("/v1/accounts", {"name": "x", "currency": "USD"}, key=key)
        assert error_code(r) == "invalid_request"


def test_concurrent_retries_apply_once(db_path: Path, clock: FakeClock) -> None:
    """Twenty clients send the same keyed transfer at the same moment."""
    app = create_app(str(db_path), clock=clock)
    # Clients are used without `with`, so no lifespan shutdown closes the shared database.
    h = Api(TestClient(app), clock, db_path)
    alice = h.account(funded=1_000)
    bob = h.account()
    barrier = threading.Barrier(20)
    results: list[tuple[int, bytes]] = []
    lock = threading.Lock()

    def send() -> None:
        client = TestClient(app)
        barrier.wait()
        r = client.post(
            "/v1/transfers",
            json={"from_account_id": alice, "to_account_id": bob, "amount": 100},
            headers={"Idempotency-Key": "same"},
        )
        with lock:
            results.append((r.status_code, r.content))

    threads = [threading.Thread(target=send) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {status for status, _ in results} == {201}
    assert len({body for _, body in results}) == 1
    assert h.balance(alice)["balance"] == 900
    app.state.db.close()
