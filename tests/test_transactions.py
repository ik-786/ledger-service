from __future__ import annotations

from ledger.schemas import MAX_AMOUNT, MAX_BALANCE

from .conftest import Api, error_code


def test_transfer_moves_money_and_records_both_sides(api: Api) -> None:
    alice = api.account(funded=1_000)
    bob = api.account()
    r = api.transfer(alice, bob, 250, description="rent share")
    assert r.status_code == 201
    txn = r.json()
    assert txn["kind"] == "transfer"
    assert txn["currency"] == "USD"
    assert txn["description"] == "rent share"
    assert [(e["account_id"], e["amount"], e["balance_after"]) for e in txn["entries"]] == [
        (alice, -250, 750),
        (bob, 250, 250),
    ]
    assert api.balance(alice)["balance"] == 750
    assert api.balance(bob)["balance"] == 250
    assert api.get(f"/v1/transactions/{txn['id']}").json() == txn


def test_insufficient_funds_changes_nothing(api: Api) -> None:
    alice = api.account(funded=100)
    bob = api.account()
    r = api.transfer(alice, bob, 101)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "insufficient_funds"
    assert (body["available"], body["required"]) == (100, 101)
    assert api.balance(alice)["balance"] == 100
    assert api.balance(bob)["balance"] == 0
    assert api.get(f"/v1/accounts/{alice}/entries").json()["data"][0]["amount"] == 100


def test_exact_balance_can_be_spent(api: Api) -> None:
    alice = api.account(funded=100)
    assert api.transfer(alice, api.account(), 100).status_code == 201
    assert api.balance(alice)["balance"] == 0


def test_negative_allowed_accounts_can_go_below_zero(api: Api) -> None:
    settlement = api.account(allow_negative=True)
    alice = api.account()
    assert api.transfer(settlement, alice, 5_000).status_code == 201
    assert api.balance(settlement)["balance"] == -5_000


def test_transfer_rules(api: Api) -> None:
    alice = api.account(funded=100)
    euro = api.account("EUR")
    assert error_code(api.transfer(alice, alice, 1)) == "same_account"
    assert error_code(api.transfer(alice, euro, 1)) == "currency_mismatch"
    r = api.transfer(alice, "acc_nope", 1)
    assert r.status_code == 404
    assert error_code(r) == "account_not_found"


def test_amount_must_be_positive_integer(api: Api) -> None:
    alice = api.account(funded=100)
    bob = api.account()
    for amount in (0, -5, 1.5, "10", True, MAX_AMOUNT + 1):
        r = api.transfer(alice, bob, amount)  # type: ignore[arg-type]
        assert r.status_code == 400, amount
        assert error_code(r) == "invalid_request"


def test_journal_with_three_legs(api: Api) -> None:
    payer = api.account(funded=1_000)
    merchant = api.account()
    fees = api.account()
    r = api.post(
        "/v1/transactions",
        {
            "legs": [
                {"account_id": payer, "amount": -1_000},
                {"account_id": merchant, "amount": 970},
                {"account_id": fees, "amount": 30},
            ],
            "description": "card payment with 3% fee",
        },
    )
    assert r.status_code == 201
    assert r.json()["kind"] == "journal"
    assert [api.balance(a)["balance"] for a in (payer, merchant, fees)] == [0, 970, 30]


def test_journal_rules(api: Api) -> None:
    a = api.account(funded=100)
    b = api.account()

    def journal(*legs: tuple[str, int]) -> object:
        return api.post(
            "/v1/transactions", {"legs": [{"account_id": x, "amount": y} for x, y in legs]}
        )

    r = journal((a, -50), (b, 40))
    assert error_code(r) == "unbalanced_transaction"
    assert r.json()["imbalance"] == -10
    assert error_code(journal((a, -50), (a, 50))) == "duplicate_account"
    assert error_code(journal((a, -50))) == "invalid_request"
    assert error_code(journal((a, 0), (b, 0))) == "invalid_request"
    assert error_code(journal((a, -500), (b, 500))) == "insufficient_funds"
    assert api.balance(a)["balance"] == 100


def test_checks_run_in_a_fixed_order(api: Api) -> None:
    """Several broken rules at once always report the same, earliest one."""
    a = api.account(funded=10)
    euro = api.account("EUR")
    legs = [{"account_id": a, "amount": -500}, {"account_id": euro, "amount": 400}]
    assert error_code(api.post("/v1/transactions", {"legs": legs})) == "unbalanced_transaction"
    legs[1]["amount"] = 500
    assert error_code(api.post("/v1/transactions", {"legs": legs})) == "currency_mismatch"
    legs[1]["account_id"] = "acc_missing"
    assert error_code(api.post("/v1/transactions", {"legs": legs})) == "account_not_found"


def test_balance_limit(api: Api) -> None:
    source = api.account(allow_negative=True)
    whale = api.account()
    for _ in range(MAX_BALANCE // MAX_AMOUNT):
        assert api.transfer(source, whale, MAX_AMOUNT).status_code == 201
    assert error_code(api.transfer(source, whale, 1)) == "balance_limit_exceeded"


def test_unknown_transaction_is_404(api: Api) -> None:
    assert error_code(api.get("/v1/transactions/txn_nope")) == "transaction_not_found"
