-- Prevent two paper workers from opening duplicate positions for one strategy
-- and symbol. Existing closed trade history is unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS positions_one_open_symbol_per_algo_idx
ON positions (algo_id, symbol)
WHERE status = 'open';
