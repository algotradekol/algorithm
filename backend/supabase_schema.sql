-- Run this once in the Supabase SQL editor (Project -> SQL Editor -> New query)

create table if not exists broker_tokens (
    broker text primary key,
    access_token text not null,
    refresh_token text,
    access_token_updated_at timestamptz,
    refresh_token_updated_at timestamptz,
    last_refresh_attempt_at timestamptz,
    last_refresh_error text,
    updated_at timestamptz default now()
);

alter table broker_tokens
    add column if not exists refresh_token text,
    add column if not exists access_token_updated_at timestamptz,
    add column if not exists refresh_token_updated_at timestamptz,
    add column if not exists last_refresh_attempt_at timestamptz,
    add column if not exists last_refresh_error text;

create table if not exists fyers_token_refresh_logs (
    id bigserial primary key,
    attempted_at timestamptz default now(),
    status text not null check (status in ('success', 'failed')),
    error text
);
create index if not exists idx_fyers_token_refresh_logs_attempted_at on fyers_token_refresh_logs (attempted_at desc);

create table if not exists charges_config (
    id int primary key default 1,
    brokerage_flat numeric default 20.0,
    brokerage_pct numeric default 0.03,
    stt_pct numeric default 0.025,
    exchange_pct numeric default 0.00297,
    sebi_pct numeric default 0.0001,
    gst_pct numeric default 18.0,
    stamp_duty_pct numeric default 0.003
);
insert into charges_config (id) values (1) on conflict (id) do nothing;

create table if not exists algo_state (
    algo_id text primary key,
    cash numeric not null,
    trade_count_today int default 0,
    buy_count_today int default 0,
    sell_count_today int default 0,
    trading_date date default current_date
);

create table if not exists positions (
    id bigserial primary key,
    algo_id text not null,
    symbol text not null,
    side text not null,
    qty int not null,
    entry_price numeric not null,
    sl_price numeric not null,
    target_price numeric not null,
    highest_price numeric,
    lowest_price numeric,
    trailing_sl_active boolean default false,
    status text not null default 'open',
    entry_time timestamptz not null
);
create index if not exists idx_positions_algo_status on positions (algo_id, status);

create table if not exists trades (
    id bigserial primary key,
    algo_id text not null,
    symbol text not null,
    side text not null,
    qty int not null,
    entry_price numeric not null,
    exit_price numeric not null,
    entry_time timestamptz not null,
    exit_time timestamptz not null,
    exit_reason text,
    brokerage numeric,
    stt numeric,
    exchange_charges numeric,
    sebi_charges numeric,
    gst numeric,
    stamp_duty numeric,
    total_charges numeric,
    gross_pnl numeric,
    net_pnl numeric
);
create index if not exists idx_trades_algo_time on trades (algo_id, exit_time desc);

-- Strategy settings addition: run this manually in Supabase SQL Editor before using strategy settings UI.
CREATE TABLE IF NOT EXISTS strategy_settings (
    algo_id text PRIMARY KEY,
    display_name text,

    -- Capital
    starting_capital numeric default 500000,
    capital_per_trade numeric default 50000,
    margin_multiplier numeric default 5,

    -- Risk
    target_pct numeric default 2.0,
    sl_pct numeric default 1.0,
    exit_mode text default 'fixed_target_trailing_sl',
    trailing_sl_enabled boolean default false,
    trailing_sl_trigger_pct numeric default 1.0,
    trailing_sl_distance_pct numeric default 0.5,
    max_trades_per_day int default 10,
    max_buy_trades int default 5,
    max_sell_trades int default 5,

    -- Algo 4 indicator thresholds (ignored by algo1/2/3)
    rsi_buy_threshold numeric default 55,
    rsi_sell_threshold numeric default 45,
    adx_threshold numeric default 25,
    min_volume numeric default 100000,
    min_total_value numeric default 100000000,
    ltp_min numeric default 200,
    ltp_max numeric default 4000,
    supertrend_period int default 10,
    supertrend_multiplier numeric default 3,

    updated_at timestamptz default now()
);

-- Seed default rows for all 4 algos
INSERT INTO strategy_settings (algo_id, display_name) VALUES
    ('algo1', 'Algo 1 — Opening Range Gap'),
    ('algo2', 'Algo 2 — VWAP/EMA/Volume Momentum'),
    ('algo3', 'Algo 3 — Opening Range Gap (Basic)'),
    ('algo4', 'Algo 4 — Opening Range Gap (With Indicators)')
ON CONFLICT (algo_id) DO NOTHING;

-- Algo 4 toggleable filter settings addition.
ALTER TABLE strategy_settings
    ADD COLUMN IF NOT EXISTS filter_vwap boolean default true,
    ADD COLUMN IF NOT EXISTS filter_rsi boolean default true,
    ADD COLUMN IF NOT EXISTS filter_adx boolean default true,
    ADD COLUMN IF NOT EXISTS filter_supertrend boolean default true,
    ADD COLUMN IF NOT EXISTS filter_ema20 boolean default false,
    ADD COLUMN IF NOT EXISTS filter_ema50 boolean default false,
    ADD COLUMN IF NOT EXISTS filter_volume boolean default true,
    ADD COLUMN IF NOT EXISTS filter_liquidity boolean default true,
    ADD COLUMN IF NOT EXISTS filter_price_range boolean default true;

-- Per-algo trailing stop loss settings.
ALTER TABLE strategy_settings
    ADD COLUMN IF NOT EXISTS trailing_sl_enabled boolean default false,
    ADD COLUMN IF NOT EXISTS trailing_sl_trigger_pct numeric default 1.0,
    ADD COLUMN IF NOT EXISTS trailing_sl_distance_pct numeric default 0.5;

ALTER TABLE strategy_settings
    ADD COLUMN IF NOT EXISTS exit_mode text default 'fixed_target_trailing_sl';

UPDATE strategy_settings
SET exit_mode = 'fixed_target_trailing_sl'
WHERE exit_mode IS NULL;

-- Per-position trailing stop state.
ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS highest_price numeric,
    ADD COLUMN IF NOT EXISTS lowest_price numeric,
    ADD COLUMN IF NOT EXISTS trailing_sl_active boolean default false;

-- Entry trigger audit trail: shows why each paper position/trade was opened.
ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS entry_trigger text;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS entry_trigger text;

-- Calendar/audit snapshots: stores the dashboard state date-wise for review.
CREATE TABLE IF NOT EXISTS calendar_snapshots (
    id bigserial PRIMARY KEY,
    snapshot_date date not null,
    algo_id text not null,
    display_name text,
    summary jsonb,
    positions jsonb,
    trades jsonb,
    scan_results jsonb,
    settings jsonb,
    engine_status jsonb,
    fyers_status jsonb,
    note text,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    UNIQUE (snapshot_date, algo_id)
);

CREATE INDEX IF NOT EXISTS idx_calendar_snapshots_date
    ON calendar_snapshots (snapshot_date desc, algo_id);

-- Trade timestamps are retained in calendar snapshot JSON and displayed in
-- the calendar. These statements are safe for databases created by older app versions.
ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS entry_time timestamptz;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS entry_time timestamptz,
    ADD COLUMN IF NOT EXISTS exit_time timestamptz;

CREATE INDEX IF NOT EXISTS idx_trades_entry_time
    ON trades (algo_id, entry_time desc);

-- Stored OHLCV candles fetched by the History graph endpoint.
CREATE TABLE IF NOT EXISTS market_candles (
    id bigserial PRIMARY KEY,
    symbol text not null,
    resolution text not null,
    candle_time timestamptz not null,
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    volume numeric,
    source text default 'fyers_history',
    raw jsonb,
    created_at timestamptz default now(),
    UNIQUE (symbol, resolution, candle_time)
);

CREATE INDEX IF NOT EXISTS idx_market_candles_symbol_time
    ON market_candles (symbol, resolution, candle_time desc);

-- AI assistant chat memory. Run manually in Supabase SQL Editor before using the assistant.
CREATE TABLE IF NOT EXISTS ai_chat_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text not null,
    title text default 'New chat',
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

CREATE TABLE IF NOT EXISTS ai_chat_messages (
    id bigserial PRIMARY KEY,
    session_id uuid references ai_chat_sessions(id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    context jsonb,
    created_at timestamptz default now()
);

CREATE INDEX IF NOT EXISTS idx_ai_chat_sessions_user_updated on ai_chat_sessions (user_id, updated_at desc);
CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_session_created on ai_chat_messages (session_id, created_at asc);

-- Row Level Security: since only the backend (using the service role
-- key) writes to these tables, and only your authenticated frontend
-- session reads via the backend API (never directly from the
-- browser), RLS can stay off for these. If you ever query these
-- tables directly from the frontend with the anon key instead of
-- going through the FastAPI backend, turn RLS on and add policies
-- restricting access to your own user id.
