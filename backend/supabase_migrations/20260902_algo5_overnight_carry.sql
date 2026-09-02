-- Persist the opt-in Silver Micro 2.0 paper/backtest overnight setting.
alter table public.strategy_settings
  add column if not exists overnight_carry_enabled boolean not null default false;
