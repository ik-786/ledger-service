-- Responses to requests that carried an Idempotency-Key header. The row is
-- written in the same database transaction as the effect, so a key is stored
-- if and only if the change it describes was committed.
CREATE TABLE idempotency_keys (
    key           TEXT    PRIMARY KEY,
    request_hash  TEXT    NOT NULL,
    status_code   INTEGER NOT NULL,
    response_body TEXT    NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE INDEX idempotency_keys_by_age ON idempotency_keys (created_at);
