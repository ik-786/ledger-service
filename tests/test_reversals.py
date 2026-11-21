from __future__ import annotations

from .conftest import Api, error_code


def test_reversal_mirrors_the_original(api: Api) -> None:
    alice = api.account(funded=500)
    bob = api.account()
    original = api.transfer(alice, bob, 200).json()
    r = api.post(f"/v1/transactions/{original['id']}/reverse", {"description": "duplicate charge"})
    assert r.status_code == 201
    reversal = r.json()
    assert reversal["kind"] == "reversal"
    assert reversal["reverses_id"] == original["id"]
    assert reversal["description"] == "duplicate charge"
    assert sorted((e["account_id"], e["amount"]) for e in reversal["entries"]) == sorted(
        (e["account_id"], -e["amount"]) for e in original["entries"]
    )
    assert api.balance(alice)["balance"] == 500
    assert api.balance(bob)["balance"] == 0
    assert api.get(f"/v1/transactions/{original['id']}").json()["reversed_by"] == reversal["id"]


def test_original_entries_stay_in_history(api: Api) -> None:
    alice = api.account(funded=500)
    original = api.transfer(alice, api.account(), 200).json()
    api.post(f"/v1/transactions/{original['id']}/reverse")
    amounts = [e["amount"] for e in api.get(f"/v1/accounts/{alice}/entries").json()["data"]]
    assert amounts == [200, -200, 500]  # newest first; nothing was edited away


def test_reverse_once_only(api: Api) -> None:
    alice = api.account(funded=500)
    original = api.transfer(alice, api.account(), 200).json()
    reversal = api.post(f"/v1/transactions/{original['id']}/reverse").json()
    r = api.post(f"/v1/transactions/{original['id']}/reverse")
    assert r.status_code == 409
    assert error_code(r) == "already_reversed"
    assert r.json()["reversed_by"] == reversal["id"]
    r = api.post(f"/v1/transactions/{reversal['id']}/reverse")
    assert error_code(r) == "cannot_reverse_reversal"


def test_reversal_needs_funds_in_the_receiving_account(api: Api) -> None:
    alice = api.account(funded=500)
    bob = api.account()
    carol = api.account()
    original = api.transfer(alice, bob, 200).json()
    api.transfer(bob, carol, 150)
    r = api.post(f"/v1/transactions/{original['id']}/reverse")
    assert error_code(r) == "insufficient_funds"
    assert r.json()["account_id"] == bob
    assert api.balance(bob)["balance"] == 50


def test_held_funds_block_a_reversal(api: Api) -> None:
    alice = api.account(funded=500)
    bob = api.account()
    original = api.transfer(alice, bob, 200).json()
    api.hold(bob, alice, 150)
    assert (
        error_code(api.post(f"/v1/transactions/{original['id']}/reverse")) == "insufficient_funds"
    )


def test_refund_a_capture(api: Api) -> None:
    card = api.account(funded=1_000)
    shop = api.account()
    hold = api.hold(card, shop, 300).json()
    captured = api.post(f"/v1/holds/{hold['id']}/capture").json()
    r = api.post(
        f"/v1/transactions/{captured['transaction_id']}/reverse", {"description": "refund"}
    )
    assert r.status_code == 201
    assert api.balance(card)["balance"] == 1_000
    assert api.balance(shop)["balance"] == 0
    assert api.get(f"/v1/holds/{hold['id']}").json()["status"] == "captured"


def test_reverse_unknown_transaction(api: Api) -> None:
    assert error_code(api.post("/v1/transactions/txn_x/reverse")) == "transaction_not_found"
