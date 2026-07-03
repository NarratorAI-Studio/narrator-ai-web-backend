from __future__ import annotations


WALLET_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wallet_accounts (
    wallet_account_id TEXT PRIMARY KEY,
    web_tenant_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    available_balance NUMERIC(18, 2) NOT NULL DEFAULT 0
        CHECK (available_balance >= 0),
    frozen_balance NUMERIC(18, 2) NOT NULL DEFAULT 0
        CHECK (frozen_balance >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (web_tenant_id, web_user_id)
);

CREATE TABLE IF NOT EXISTS wallet_quotes (
    quote_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'EXPIRED')),
    web_tenant_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    web_order_id TEXT NOT NULL,
    template_id INTEGER NOT NULL,
    combo_key TEXT NOT NULL,
    hard_price NUMERIC(18, 2) NOT NULL CHECK (hard_price > 0),
    amount_points NUMERIC(18, 2) NOT NULL CHECK (amount_points > 0),
    pricing_rule_version INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    pricing_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (web_order_id)
);

CREATE INDEX IF NOT EXISTS wallet_quotes_owner_order_idx
    ON wallet_quotes (web_tenant_id, web_user_id, web_order_id);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    wallet_transaction_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('FROZEN', 'CONFIRMED', 'REFUNDED')),
    quote_id TEXT NOT NULL REFERENCES wallet_quotes (quote_id),
    wallet_account_id TEXT NOT NULL REFERENCES wallet_accounts (wallet_account_id),
    web_tenant_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    web_order_id TEXT NOT NULL,
    amount_points NUMERIC(18, 2) NOT NULL CHECK (amount_points > 0),
    pricing_rule_version INTEGER NOT NULL,
    frozen_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    refunded_at TIMESTAMPTZ,
    refund_reason_code TEXT,
    refund_reason_message TEXT,
    correlation JSONB NOT NULL DEFAULT '{}'::jsonb,
    refund_correlation JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (web_order_id)
);

CREATE INDEX IF NOT EXISTS wallet_transactions_owner_order_idx
    ON wallet_transactions (web_tenant_id, web_user_id, web_order_id);

CREATE TABLE IF NOT EXISTS wallet_ledger_entries (
    ledger_entry_id TEXT PRIMARY KEY,
    wallet_transaction_id TEXT NOT NULL
        REFERENCES wallet_transactions (wallet_transaction_id),
    wallet_account_id TEXT NOT NULL REFERENCES wallet_accounts (wallet_account_id),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('FREEZE', 'CONFIRM', 'REFUND')),
    amount_points NUMERIC(18, 2) NOT NULL CHECK (amount_points > 0),
    balance_available_after NUMERIC(18, 2) NOT NULL
        CHECK (balance_available_after >= 0),
    balance_frozen_after NUMERIC(18, 2) NOT NULL
        CHECK (balance_frozen_after >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wallet_ledger_transaction_idx
    ON wallet_ledger_entries (wallet_transaction_id);

CREATE TABLE IF NOT EXISTS wallet_idempotency_records (
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_status INTEGER NOT NULL CHECK (
        response_status >= 100 AND response_status <= 599
    ),
    response_body JSONB NOT NULL,
    web_tenant_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    web_order_id TEXT NOT NULL,
    wallet_transaction_id TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_replay_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation, idempotency_key)
);

CREATE INDEX IF NOT EXISTS wallet_idempotency_owner_order_idx
    ON wallet_idempotency_records (web_tenant_id, web_user_id, web_order_id);

CREATE INDEX IF NOT EXISTS wallet_idempotency_first_seen_idx
    ON wallet_idempotency_records (first_seen_at);
"""
