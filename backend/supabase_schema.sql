-- Run this once in the Supabase SQL editor (Project -> SQL Editor -> New query)

create table if not exists broker_tokens (
    broker text primary key,
    access_token text not null,
    updated_at timestamptz default now()
);

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

-- Row Level Security: since only the backend (using the service role
-- key) writes to these tables, and only your authenticated frontend
-- session reads via the backend API (never directly from the
-- browser), RLS can stay off for these. If you ever query these
-- tables directly from the frontend with the anon key instead of
-- going through the FastAPI backend, turn RLS on and add policies
-- restricting access to your own user id.
