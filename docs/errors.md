# Errors

Every error is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem document with
`Content-Type: application/problem+json`. Branch on `code`; it never changes. `detail` is for humans
and may be reworded.

```json
{
  "type": "https://github.com/ik-786/ledger-service/blob/main/docs/errors.md#insufficient_funds",
  "title": "Insufficient funds",
  "status": 422,
  "code": "insufficient_funds",
  "detail": "account 'acc_1ffb...' has 400 available and needs 500",
  "request_id": "5959234...",
  "account_id": "acc_1ffb...",
  "available": 400,
  "required": 500
}
```

When a request breaks several rules, the checks run in a fixed order and the first one is reported:
request validation, then `same_account`, `duplicate_account` and `unbalanced_transaction`, then the
`*_not_found` errors in the order the accounts appear, then `currency_mismatch`, then the funds and
limit checks for each leg in order. The same request against the same state therefore always gets the
same error.

None of these errors change anything. A refused write rolls back completely, including its
idempotency key.

## Request errors

### invalid_request
`400`. The body failed validation (an `errors` list names each field), is not JSON, is not a JSON
object, or a query parameter, cursor or `Idempotency-Key` header is malformed. Amounts must be JSON
integers: `"10"`, `10.0` and `true` are all refused.

### body_too_large
`413`. The body is over 64 KiB.

### unsupported_media_type
`415`. A body was sent without `Content-Type: application/json`.

### route_not_found
`404`. No such path.

### method_not_allowed
`405`. The path exists but not with this method. The `Allow` header lists the methods it takes.

## Not found

### account_not_found
`404`. An account id in the path or the body does not exist.

### transaction_not_found
`404`.

### hold_not_found
`404`.

## Ledger rules

### insufficient_funds
`422`. A debit, hold or reversal would take an account below zero available funds. Includes
`account_id`, `available` and `required`. Accounts created with `allow_negative: true` never get this.

### same_account
`422`. A transfer or hold names the same account as source and destination.

### duplicate_account
`422`. An account appears in more than one leg of a transaction. Combine the legs instead.

### unbalanced_transaction
`422`. The legs of a transaction do not sum to zero. Includes `imbalance`.

### currency_mismatch
`422`. The accounts in a transaction or hold do not all use the same currency. The ledger does not
convert currencies.

### balance_limit_exceeded
`422`. The posting would push an account's balance past ±10^18 minor units. The limit keeps every
balance and sum well inside SQLite's 64-bit integers.

### capture_exceeds_hold
`422`. A capture asks for more than the hold reserved. Includes `held`.

### cannot_reverse_reversal
`422`. Reversals are final. To undo one, post a new transfer.

### idempotency_key_reused
`422`. The `Idempotency-Key` was already used, within the last 24 hours, for a different request
(a different method, path or body). Follows the IETF `Idempotency-Key` header draft.

## State conflicts

### already_reversed
`409`. The transaction already has a reversal. Includes `reversed_by`.

### hold_expired
`409`. The hold passed its `expires_at` and its funds were released. Includes `hold_status`.

### hold_not_active
`409`. The hold was already captured or voided. Includes `hold_status`.

### constraint_violation
`409`. A database constraint refused the write. The service checks every rule before it writes, so
this only appears if one of those checks has a bug. The database stays consistent either way.

## Server errors

### database_busy
`503` with `Retry-After: 1`. The write lock was not free within 5 seconds. Retrying with the same
`Idempotency-Key` is safe.

### internal_error
`500`. An unexpected error. The details go to the server log under the `request_id`.
