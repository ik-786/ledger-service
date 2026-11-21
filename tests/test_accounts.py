from __future__ import annotations

import pytest

from .conftest import Api, error_code


def test_create_and_read_account(api: Api) -> None:
    r = api.post("/v1/accounts", {"name": "Alice wallet", "currency": "INR"})
    assert r.status_code == 201
    created = r.json()
    assert created["id"].startswith("acc_")
    assert created["balance"] == created["held"] == created["available"] == 0
    assert created["allow_negative"] is False
    assert created["created_at"] == "2026-01-01T00:00:00.000Z"
    assert api.get(f"/v1/accounts/{created['id']}").json() == created


def test_unknown_account_is_404(api: Api) -> None:
    r = api.get("/v1/accounts/acc_missing")
    assert r.status_code == 404
    assert error_code(r) == "account_not_found"


@pytest.mark.parametrize(
    "body, field",
    [
        ({"name": "x", "currency": "XYZ"}, "currency"),
        ({"name": "", "currency": "USD"}, "name"),
        ({"currency": "USD"}, "name"),
        ({"name": "x", "currency": "USD", "allow_negative": "true"}, "allow_negative"),
        ({"name": "x", "currency": "USD", "allow_negative": 1}, "allow_negative"),
        ({"name": "x", "currency": "USD", "balance": 100}, "balance"),
    ],
)
def test_account_validation(api: Api, body: dict[str, object], field: str) -> None:
    r = api.post("/v1/accounts", body)
    assert r.status_code == 400
    assert error_code(r) == "invalid_request"
    assert field in {e["field"] for e in r.json()["errors"]}


def test_list_accounts_pages_in_creation_order(api: Api) -> None:
    ids = [api.account(name=f"a{i}") for i in range(7)]
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        url = "/v1/accounts?limit=3" + (f"&cursor={cursor}" if cursor else "")
        page = api.get(url).json()
        seen += [a["id"] for a in page["data"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids
    assert pages == 3


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=abc", "limit=-1"])
def test_bad_page_size(api: Api, query: str) -> None:
    r = api.get(f"/v1/accounts?{query}")
    assert r.status_code == 400
    assert error_code(r) == "invalid_request"
