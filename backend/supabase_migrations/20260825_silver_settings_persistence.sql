-- Run once in Supabase SQL Editor for each project that shares this schema.
-- Idempotent: safe to re-run. These fields are required for Silver settings
-- to survive a page refresh/redeploy instead of falling back to defaults.
ALTER TABLE public.strategy_settings
    ADD COLUMN IF NOT EXISTS order_type text DEFAULT 'MARKET',
    ADD COLUMN IF NOT EXISTS parallel_paper_enabled boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS silver_breakout_points numeric DEFAULT 150,
    ADD COLUMN IF NOT EXISTS sl_points numeric DEFAULT 100,
    ADD COLUMN IF NOT EXISTS target_points numeric DEFAULT 300,
    ADD COLUMN IF NOT EXISTS tsl_activate_points numeric DEFAULT 100,
    ADD COLUMN IF NOT EXISTS tsl_profit_step_points numeric DEFAULT 100,
    ADD COLUMN IF NOT EXISTS tsl_lock_step_points numeric DEFAULT 50,
    ADD COLUMN IF NOT EXISTS tsl_trigger_points numeric DEFAULT 100,
    ADD COLUMN IF NOT EXISTS tsl_distance_points numeric DEFAULT 50,
    ADD COLUMN IF NOT EXISTS silver_lots integer DEFAULT 1,
    ADD COLUMN IF NOT EXISTS silver_buy_plan text DEFAULT 'reference_breakout';
