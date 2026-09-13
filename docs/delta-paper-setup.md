# Delta paper setup

Delta runs as paper-only strategies under the main Delta tab. The workspace is
split into two metal groups:

- Gold: `PAXGUSD`, timeframes `5m`, `7m`, `15m`, `30m`, `1h`, and `4h`.
- Silver: `SLVONUSD` by default, timeframes `5m`, `15m`, `30m`, `1h`, and `4h`.

Gold uses the Delta PAXG EMA20 plus volume EMA20 strategy. Silver uses the
normal Silver Micro EMA20/red-chain entry behavior on Delta silver candles.
Both groups are continuous paper simulations with no daily square-off and no
Delta live order placement.

## Supabase

Run `backend/supabase_migrations/20260909_delta_paper.sql` in the Supabase SQL
Editor before enabling Delta. It creates isolated JSON state and trade tables
with RLS and a service-role-only closure function. Existing FYERS tables and
settings are not migrated.

The backend uses the existing Supabase service-role connection. State keys
include exchange, symbol, timeframe, paper mode, and `BROKER_KEY_SUFFIX`. Use
different `BROKER_KEY_SUFFIX` values for dev and production when sharing one
Supabase project. Run one Railway replica with one Uvicorn worker for each
storage namespace.

## Railway backend environment

```dotenv
DELTA_PAPER_ENABLED=true
DELTA_EXCHANGE=india
DELTA_GOLD_SYMBOL=PAXGUSD
DELTA_SILVER_SYMBOL=SLVONUSD
DELTA_PROXY_URL=http://PROXY_USERNAME:URL_ENCODED_PASSWORD@PROXY_HOST:PROXY_PORT

# Optional read-only account credentials for Verify API connection and the
# Positions & Orders live-account view. These never enable live Delta trading.
DELTA_API_KEY=YOUR_INDIA_API_KEY
DELTA_API_SECRET=YOUR_INDIA_API_SECRET

# Optional comma-separated deployment gate. Empty = show everything.
DELTA_HIDDEN_SECTIONS=
```

Supported `DELTA_HIDDEN_SECTIONS` keywords:

`delta`, `gold`, `silver`, `overview`, `activity`, `backtest`,
`gold5m`, `gold7m`, `gold15m`, `gold30m`, `gold1h`, `gold4h`,
`silveroverview`, `silveractivity`, `silverbacktest`,
`silver5m`, `silver15m`, `silver30m`, `silver1h`, `silver4h`.

Examples:

```dotenv
# Client sees only the selected Gold strategies, with no activity/backtest.
DELTA_HIDDEN_SECTIONS=gold5m,gold7m,gold4h,activity,backtest,silver

# Developer sees all Gold, but only Silver 15m and 1h.
DELTA_HIDDEN_SECTIONS=silver5m,silver30m,silver4h
```

Unknown keywords fail closed for Delta and surface a configuration error. The
visibility setting is per deployment, so client/dev separation should be done
with separate Railway services or separate environment values.

## Proxy and API key

`DELTA_PROXY_URL` is optional. Set it to the existing Google VM HTTP CONNECT
proxy address when Delta traffic must leave from the same static egress IP used
for exchange whitelisting. Both REST and WebSocket use the explicit proxy; there
is no direct bypass when it is configured.

The VM must allow HTTPS CONNECT on port 443 to:

- `api.india.delta.exchange`
- `public-socket.india.delta.exchange`

Whitelist the VM public egress IP in Delta separately. The FYERS whitelist does
not automatically apply to Delta, even if the same VM proxy is reused.

## Strategy defaults

Current Delta defaults for new normalized settings:

- Breakout offset: `3`
- Initial stop loss: `15`
- Final target: `50`
- Breakeven TSL activation: `15`
- Three-candle TSL buffer: `3` for Gold only
- Lots per trade: `1`
- Post-exit rest: `5` minutes
- Scan starts ON and paper trading starts OFF

Gold supports fixed target plus SL, target plus breakeven SL, and three-candle
TSL. Silver supports fixed target plus SL and target plus breakeven SL, matching
normal Silver Micro behavior without Gold's volume filter or three-candle TSL.
New Silver settings default to target plus breakeven SL, and the Silver settings
panel includes a `Use Tradetron defaults` button for restoring the normal Fyers
Silver Micro values: offset `200`, SL `200`, target `2000`, breakeven activation
`500`, lots `1`, and breakeven mode.

Manual exits, SL exits, trailing exits, and targets start the per-timeframe
post-exit rest timer when it is greater than zero. Set rest to `0` for immediate
re-entry eligibility after those exits. Reversal exits bypass this timer.
Dashboards include presets, a custom-minute pause, and a Resume action.

## Frontend

Keep `NEXT_PUBLIC_API_URL` pointed at Railway. No frontend Delta env variable is
needed. The frontend reads `/api/delta/capabilities` and only renders the Gold
or Silver groups, timeframes, activity view, and backtest view enabled by the
backend.

The Delta tab contains separate Gold and Silver groups. Each group has its own
dashboard, timeframe tabs, Positions & Orders tab, and backtest tab when enabled.
Gold and Silver positions, settings, cooldowns, P&L, references, CSV exports,
and backtests are stored independently.

Quantities display as lots. One displayed lot equals one Delta exchange
quantity unit. Paper P&L is calculated in the product quote currency and also
shows INR where Delta India USD conversion is available. Margin is an estimate
captured at entry from product metadata; it is not an actual exchange margin
reservation.

The Positions & Orders view can show paper simulation rows or read-only live
account rows for the selected metal. It never places or modifies Delta orders.

## Backtest

The Delta Backtest tab supports the enabled timeframes for the selected metal.
It fetches exchange reference candles with warmup data and replays completed
1-minute execution candles. The 7-minute Gold timeframe is built from complete
1-minute OHLCV windows on UTC epoch boundaries.

Backtests run in memory and do not write to Supabase or change paper settings.
Reports include the chart, closed trades, any range-end open position, reference
facts, exit/protection facts, lots, margin estimates, INR values, diagnostics,
settings, and CSV export.

The replay uses an assumed 1-minute OHLC path, so intraminute fills are simulated
rather than exact historical ticks. Internal no-trade history gaps are filled as
flat zero-volume candles; duplicate or conflicting candles still fail explicitly.

## Verification

```powershell
cd backend
python -m tests.smoke_delta_paper
python -m tests.smoke_delta_backtest
python -m tests.smoke_delta_controls
python -m tests.smoke_delta_reporting
python -m tests.smoke_delta_multiframe
python -m tests.smoke_delta_silver
python -m tests.smoke_silver_logic
python -m tests.smoke_silver_micro_2
python -m tests.smoke_silver_v_micro
python -m compileall -q app

cd ..\frontend
npx tsc --noEmit
npm run build

cd ..
git diff --check
```

No extra SQL migration is needed for new Delta Gold/Silver timeframe settings or
snapshot fields because they use the existing JSON state and trade tables.

Official references:

- https://docs.delta.exchange/
- https://api.india.delta.exchange/v2/products/PAXGUSD
- https://api.india.delta.exchange/v2/products/SLVONUSD
