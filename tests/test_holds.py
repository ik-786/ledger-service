from __future__ import annotations

from .conftest import Api, error_code


def test_hold_reserves_funds_without_moving_them(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    r = api.hold(card, shop, 700)
    assert r.status_code == 201
    hold = r.json()
    assert hold["status"] == "active"
    assert hold["expires_at"] == "2026-01-08T00:00:00.000Z"  # default: 7 days
    assert api.balance(card) == {"balance": 1_000, "held": 700, "available": 300}
    assert api.balance(shop)["balance"] == 0


def test_held_funds_cannot_be_spent_twice(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    api.hold(card, shop, 700)
    assert error_code(api.transfer(card, shop, 301)) == "insufficient_funds"
    assert error_code(api.hold(card, shop, 301)) == "insufficient_funds"
    assert api.transfer(card, shop, 300).status_code == 201
    assert api.balance(card) == {"balance": 700, "held": 700, "available": 0}


def test_partial_capture_releases_the_rest(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    hold = api.hold(card, shop, 700).json()
    r = api.post(f"/v1/holds/{hold['id']}/capture", {"amount": 450})
    assert r.status_code == 200
    captured = r.json()
    assert captured["status"] == "captured"
    assert captured["captured_amount"] == 450
    txn = api.get(f"/v1/transactions/{captured['transaction_id']}").json()
    assert txn["kind"] == "capture"
    assert txn["hold_id"] == hold["id"]
    assert api.balance(card) == {"balance": 550, "held": 0, "available": 550}
    assert api.balance(shop)["balance"] == 450


def test_full_capture_by_default(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    hold = api.hold(card, shop, 1_000).json()
    assert api.post(f"/v1/holds/{hold['id']}/capture").json()["captured_amount"] == 1_000
    assert api.balance(card)["balance"] == 0


def test_capture_limits_and_single_use(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    hold = api.hold(card, shop, 500).json()
    url = f"/v1/holds/{hold['id']}/capture"
    assert error_code(api.post(url, {"amount": 501})) == "capture_exceeds_hold"
    assert api.post(url, {"amount": 500}).status_code == 200
    r = api.post(url, {"amount": 1})
    assert r.status_code == 409
    assert error_code(r) == "hold_not_active"
    assert r.json()["hold_status"] == "captured"


def test_void_releases_funds(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    hold = api.hold(card, shop, 600).json()
    r = api.post(f"/v1/holds/{hold['id']}/void")
    assert r.status_code == 200
    assert r.json()["status"] == "voided"
    assert api.balance(card) == {"balance": 1_000, "held": 0, "available": 1_000}
    assert error_code(api.post(f"/v1/holds/{hold['id']}/capture")) == "hold_not_active"
    assert error_code(api.post(f"/v1/holds/{hold['id']}/void")) == "hold_not_active"


def test_expired_hold_releases_funds_and_cannot_be_captured(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    hold = api.hold(card, shop, 600, seconds=60).json()
    api.clock.advance(59.999)
    assert api.balance(card)["held"] == 600
    api.clock.advance(0.001)  # expiry is inclusive: at expires_at the hold is gone
    assert api.balance(card) == {"balance": 1_000, "held": 0, "available": 1_000}
    assert api.get(f"/v1/holds/{hold['id']}").json()["status"] == "expired"
    r = api.post(f"/v1/holds/{hold['id']}/capture")
    assert r.status_code == 409
    assert error_code(r) == "hold_expired"
    assert error_code(api.post(f"/v1/holds/{hold['id']}/void")) == "hold_expired"
    assert api.transfer(card, shop, 1_000).status_code == 201


def test_capture_uses_the_reserved_funds_even_when_rest_is_spent(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    hold = api.hold(card, shop, 400).json()
    assert api.transfer(card, shop, 600).status_code == 201
    assert api.balance(card)["available"] == 0
    assert api.post(f"/v1/holds/{hold['id']}/capture").status_code == 200
    assert api.balance(card) == {"balance": 0, "held": 0, "available": 0}


def test_hold_rules(api: Api) -> None:
    card = api.account(funded=100)
    euro = api.account("EUR")
    assert error_code(api.hold(card, card, 1)) == "same_account"
    assert error_code(api.hold(card, euro, 1)) == "currency_mismatch"
    assert error_code(api.hold(card, "acc_x", 1)) == "account_not_found"
    assert error_code(api.hold(card, api.account(), 1, seconds=0)) == "invalid_request"
    assert error_code(api.hold(card, api.account(), 1, seconds=31 * 86400)) == "invalid_request"
    assert error_code(api.get("/v1/holds/hold_x")) == "hold_not_found"
