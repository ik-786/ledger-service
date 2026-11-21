#!/usr/bin/env bash
# End-to-end check against a running server, e.g. the Docker image in CI.
set -euo pipefail
BASE=${1:-http://127.0.0.1:8000}

for _ in $(seq 1 50); do
  curl -fs "$BASE/healthz" > /dev/null && break
  sleep 0.2
done

post() { curl -fsS -X POST "$BASE$1" -H 'Content-Type: application/json' "${@:3}" -d "$2"; }
field() { python3 -c "import json, sys; print(json.load(sys.stdin)['$1'])"; }

funding=$(post /v1/accounts '{"name": "funding", "currency": "USD", "allow_negative": true}' | field id)
alice=$(post /v1/accounts '{"name": "alice", "currency": "USD"}' | field id)
bob=$(post /v1/accounts '{"name": "bob", "currency": "USD"}' | field id)

deposit="{\"from_account_id\": \"$funding\", \"to_account_id\": \"$alice\", \"amount\": 1000}"
post /v1/transfers "$deposit" -H 'Idempotency-Key: smoke-deposit' > /dev/null
post /v1/transfers "$deposit" -H 'Idempotency-Key: smoke-deposit' > /dev/null  # retry, no effect
test "$(curl -fsS "$BASE/v1/accounts/$alice" | field balance)" = 1000

hold=$(post /v1/holds "{\"account_id\": \"$alice\", \"destination_account_id\": \"$bob\", \"amount\": 600}" | field id)
test "$(curl -fsS "$BASE/v1/accounts/$alice" | field available)" = 400

status=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/transfers" \
  -H 'Content-Type: application/json' \
  -d "{\"from_account_id\": \"$alice\", \"to_account_id\": \"$bob\", \"amount\": 500}")
test "$status" = 422

post "/v1/holds/$hold/capture" '{"amount": 250}' > /dev/null
test "$(curl -fsS "$BASE/v1/accounts/$alice" | field balance)" = 750
test "$(curl -fsS "$BASE/v1/accounts/$bob" | field balance)" = 250

echo "smoke test passed"
