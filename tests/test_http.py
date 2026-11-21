"""Pagination, error format and HTTP details."""

from __future__ import annotations

from .conftest import Api, error_code


def test_entries_page_newest_first_with_running_balance(api: Api) -> None:
    alice = api.account()
    bob = api.account()
    for amount in (100, 200, 300, 400, 500):
        api.fund(alice, amount)
    api.transfer(alice, bob, 50)
    page = api.get(f"/v1/accounts/{alice}/entries?limit=4").json()
    assert [(e["amount"], e["balance_after"]) for e in page["data"]] == [
        (-50, 1450),
        (500, 1500),
        (400, 1000),
        (300, 600),
    ]
    rest = api.get(f"/v1/accounts/{alice}/entries?limit=4&cursor={page['next_cursor']}").json()
    assert [(e["amount"], e["balance_after"]) for e in rest["data"]] == [(200, 300), (100, 100)]
    assert rest["next_cursor"] is None


def test_new_entries_do_not_shift_later_pages(api: Api) -> None:
    alice = api.account()
    for amount in range(1, 7):
        api.fund(alice, amount)
    first = api.get(f"/v1/accounts/{alice}/entries?limit=3").json()
    for amount in (100, 200):  # written between page requests
        api.fund(alice, amount)
    second = api.get(f"/v1/accounts/{alice}/entries?limit=3&cursor={first['next_cursor']}").json()
    assert [e["amount"] for e in first["data"] + second["data"]] == [6, 5, 4, 3, 2, 1]


def test_cursors_are_validated(api: Api) -> None:
    alice = api.account()
    bob = api.account()
    for _ in range(3):
        api.fund(alice, 1)
    cursor = api.get(f"/v1/accounts/{alice}/entries?limit=1").json()["next_cursor"]
    assert error_code(api.get(f"/v1/accounts/{bob}/entries?cursor={cursor}")) == "invalid_request"
    assert error_code(api.get(f"/v1/accounts?cursor={cursor}")) == "invalid_request"
    assert error_code(api.get(f"/v1/accounts/{alice}/entries?cursor=%%%")) == "invalid_request"
    assert (
        error_code(api.get(f"/v1/accounts/{alice}/entries?cursor=bm90IGpzb24")) == "invalid_request"
    )


def test_problem_details_shape(api: Api) -> None:
    r = api.get("/v1/accounts/acc_missing")
    body = r.json()
    assert body["type"].endswith("/docs/errors.md#account_not_found")
    assert body["title"] == "Account not found"
    assert body["status"] == 404
    assert body["detail"] == "account 'acc_missing' does not exist"
    assert body["request_id"] == r.headers["x-request-id"]


def test_request_id_is_kept_or_generated(api: Api) -> None:
    r = api.client.get("/healthz", headers={"X-Request-ID": "trace-123"})
    assert r.headers["x-request-id"] == "trace-123"
    generated = api.get("/healthz").headers["x-request-id"]
    assert len(generated) == 32


def test_unknown_routes_and_methods(api: Api) -> None:
    r = api.get("/v2/accounts")
    assert (r.status_code, error_code(r)) == (404, "route_not_found")
    r = api.client.delete("/v1/accounts")
    assert (r.status_code, error_code(r)) == (405, "method_not_allowed")
    assert "GET" in r.headers["allow"]


def test_body_must_be_a_json_object(api: Api) -> None:
    post = api.client.post
    r = post("/v1/accounts", content=b"name=x", headers={"Content-Type": "text/plain"})
    assert (r.status_code, error_code(r)) == (415, "unsupported_media_type")
    r = post("/v1/accounts", content=b"{oops", headers={"Content-Type": "application/json"})
    assert error_code(r) == "invalid_request"
    r = post("/v1/accounts", content=b"[1, 2]", headers={"Content-Type": "application/json"})
    assert error_code(r) == "invalid_request"
    r = post("/v1/accounts", content=b" " * 70_000, headers={"Content-Type": "application/json"})
    assert (r.status_code, error_code(r)) == (413, "body_too_large")


def test_health(api: Api) -> None:
    assert api.get("/healthz").json() == {"status": "ok"}
