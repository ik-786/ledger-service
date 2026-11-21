-- Accounts hold a cached balance. The entries table is the source of truth;
-- `python -m ledger check` verifies that the two agree.
CREATE TABLE accounts (
    id             TEXT    PRIMARY KEY,
    name           TEXT    NOT NULL,
    currency       TEXT    NOT NULL,
    allow_negative INTEGER NOT NULL DEFAULT 0 CHECK (allow_negative IN (0, 1)),
    balance        INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    -- Last line of defence: the service checks available funds first.
    CHECK (allow_negative = 1 OR balance >= 0)
);

CREATE TABLE holds (
    id              TEXT    PRIMARY KEY,
    account_id      TEXT    NOT NULL REFERENCES accounts (id),
    destination_id  TEXT    NOT NULL REFERENCES accounts (id),
    amount          INTEGER NOT NULL CHECK (amount > 0),
    status          TEXT    NOT NULL CHECK (status IN ('active', 'captured', 'voided')),
    captured_amount INTEGER CHECK (captured_amount IS NULL OR captured_amount > 0),
    expires_at      INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    closed_at       INTEGER
);

CREATE INDEX holds_by_account ON holds (account_id, status, expires_at);

CREATE TABLE transactions (
    id          TEXT    PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN ('transfer', 'journal', 'capture', 'reversal')),
    currency    TEXT    NOT NULL,
    description TEXT,
    -- A transaction can be reversed at most once, and a hold captured at most once.
    reverses_id TEXT    UNIQUE REFERENCES transactions (id),
    hold_id     TEXT    UNIQUE REFERENCES holds (id),
    created_at  INTEGER NOT NULL
);

CREATE TABLE entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT    NOT NULL REFERENCES transactions (id),
    account_id     TEXT    NOT NULL REFERENCES accounts (id),
    amount         INTEGER NOT NULL CHECK (amount <> 0),
    balance_after  INTEGER NOT NULL,
    created_at     INTEGER NOT NULL
);

CREATE INDEX entries_by_account ON entries (account_id, id);
CREATE INDEX entries_by_transaction ON entries (transaction_id);

-- Posted money is never edited or deleted. Corrections are new transactions.
CREATE TRIGGER entries_are_append_only_u BEFORE UPDATE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries are append-only');
END;

CREATE TRIGGER entries_are_append_only_d BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries are append-only');
END;

CREATE TRIGGER transactions_are_append_only_u BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are append-only');
END;

CREATE TRIGGER transactions_are_append_only_d BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are append-only');
END;
