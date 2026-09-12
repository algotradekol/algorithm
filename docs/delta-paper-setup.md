# Delta Gold paper setup

Two independent strategies run continuously on Delta India's `PAXGUSD` perpetual:

- Delta Gold 15 min: completed exchange 15-minute candles.
- Delta Gold 1 hr: completed exchange 1-hour candles.

Both reuse original Silver Micro's EMA20 reference selection, tick entry,
same-reference re-entry, reversal and 30-second post-stop cooldown methods.
Green closes above EMA20 supply BUY references; red closes below EMA20 supply
SELL references. Trigger distances, initial SL, final target, breakeven activation
and contracts per trade are editable separately. There is no volume filter,
EMA-wick fallback, candle-pair trailing or daily square-off.

## Supabase

Run `backend/supabase_migrations/20260909_delta_paper.sql` in the Supabase SQL
Editor before enabling Delta. It creates isolated state and trade tables with
RLS and a service-role-only function that saves position closure and its trade
journal entry atomically. Existing FYERS tables/settings are not migrated.

The backend uses the existing Supabase service-role connection. Data keys contain
exchange, symbol, timeframe, paper mode, and the existing `BROKER_KEY_SUFFIX`.
Use different `BROKER_KEY_SUFFIX` values for dev and production when sharing a DB.
Run one Railway replica with one Uvicorn worker for each storage namespace, as
this engine owns its state in one process. Do not run multiple active deployments
with the same namespace.

## Railway backend environment

```dotenv
DELTA_PAPER_ENABLED=true
DELTA_EXCHANGE=india
DELTA_GOLD_SYMBOL=PAXGUSD
DELTA_PROXY_URL=http://PROXY_USERNAME:URL_ENCODED_PASSWORD@PROXY_HOST:PROXY_PORT
# Optional comma-separated deployment gate. Supported values: delta, overview,
# gold5m, gold7m, gold15m, gold30m, gold1h, gold4h, activity, backtest.
DELTA_HIDDEN_SECTIONS=
```

Each Delta timeframe has an independent 1-minute-to-7-day post-exit rest setting. Manual exits,
fixed or trailing stops, and targets start that timer; reversal exits bypass it.
The timeframe dashboard also provides rest presets, a custom-minute pause, and
an immediate Resume action in its top controls.

`DELTA_PROXY_URL` is optional. Set it to the existing Google VM HTTP CONNECT
proxy address if all Delta traffic should use the VM's public egress IP. Both
REST and WebSocket use this explicit proxy. There is no automatic direct bypass
when a proxy is configured. The VM must allow HTTPS CONNECT on port 443 to
`api.india.delta.exchange` and `public-socket.india.delta.exchange`.

India is the default. Replace any previously configured Global environment
values with the India values above, and use credentials created on India.
India `PAXGUSD` (product 123006) and Global `PAXGUSDT` (product
277715) are different products. Both were verified through their respective
public product endpoints on 2026-09-09. Uppercasing `PAXGUSD` does not make it a
valid Global contract. The paper UI displays India's notional USD P&L and an INR
equivalent using Delta India's fixed 85 INR/USD accounting conversion. This is
not a live FX quote or a fetched account balance. Delta's official notice
says Indian customers are served by Delta India:
https://support.global.delta.exchange/support/solutions/articles/80001153662-important-update-for-delta-exchange-global-users-in-india

India public WebSocket subscriptions use
`wss://public-socket.india.delta.exchange`.

Optional read-only credentials, used by **Verify API connection** and the
**Positions & Orders > Live account (read-only)** view:

```dotenv
DELTA_API_KEY=YOUR_INDIA_API_KEY
DELTA_API_SECRET=YOUR_INDIA_API_SECRET
```

Public candles/trades do not need these credentials. Verification performs a
signed read-only wallet request. The account tab also reads positions and paginated
open/stop orders and order history. No Delta order-placement endpoint or live broker
exists in this implementation. Keep credentials in Railway backend variables;
never use `NEXT_PUBLIC_` for secrets or put them in source code.

For an IP-restricted key, whitelist the Google VM's actual external egress IP in
Delta separately. Reusing the same VM IP as FYERS is possible; the FYERS whitelist
does not automatically apply to Delta. Reserve a static external IP on the VM.
If your existing tunnel has a changing host/port, the tunnel URL still needs
maintenance even though its ultimate egress IP stays fixed.

## Frontend

Keep the existing `NEXT_PUBLIC_API_URL` pointing to Railway. No new frontend
environment variables are needed. Open Delta, then either Gold tab. Review the
settings and enable Trading separately for each strategy. Scan starts ON;
Trading starts OFF for a new configuration.

The copied defaults are offset 200, initial SL 200, TSL activation 500, final
target 2000, and one contract. These are distances in the PAXGUSD quoted price,
not INR and not ticks. Review them before enabling paper trading. A contract is
currently 0.001 PAXG; the backend verifies product metadata on startup. Paper
gross P&L is signed price difference * contracts * contract value, in nominal USD.
Estimated net deducts the product's published taker fee on entry and exit;
funding, taxes, slippage, leverage and liquidation are not simulated.

Quantity is displayed as lots: 1 displayed lot = 1 exchange contract, with no
change in sizing. New paper entries capture base margin percentage and estimated
entry margin (entry price * lots * contract value * base margin percent / 100).
This estimate excludes account leverage overrides, size tiers and fees; it is not
an actual exchange margin reservation. Old records without margin snapshots show
`--`. Live account margin is displayed only when returned by Delta, never invented
for orders. INR conversions apply only to India USD amounts.

The Positions & Orders tab defaults to Paper and offers both paper timeframes.
Paper entries fill immediately, so there are no pending entry orders. Paper stops
and targets are virtual protection; history lists simulated entry/exit events.
Switching its data source to Live account is read-only and shows all account
products; it does not enable live trading. Missing fields and failed fetches are
not represented as zero balances or confirmed empty accounts.

Both Gold paper dashboards have an inline Edit SL / Target control and an Exit
button on each open row. Edits require a fresh price and unchanged position ID,
SL and target. Protection changes persist before success is returned, retain
the original initial SL, and record a manual-edit audit. Stops must remain on
the valid side of current price; an armed breakeven stop cannot return to loss.
Pending activation stays fixed, and activation preserves a tighter manual stop.
These controls never amend or close actual Delta account orders.

Download open CSV exports the current position; Download closed CSV fetches all
closed trades up to a fixed export cutoff, not just the current page. Exports
include settings, reference snapshots, manual edits, fees, margin and INR values.
The 50,000-row safety limit fails explicitly rather than returning a partial CSV.
No additional SQL migration is needed for these JSON snapshot fields.

Times display in IST. Candle boundaries follow Delta's UTC-aligned exchange
bars. An exchange 1-hour candle can therefore display at `:30` IST, not `:00`.
Restarting the backend restores open positions and their captured protection;
settings changes apply to new trades, not existing stops/targets.

## Delta Backtest

The Delta Backtest tab supports Gold 15 min and Gold 1 hr independently of the
running paper engine. It fetches exchange reference candles with 300 warmup bars,
and replays completed 1-minute execution candles. Date selection is IST, up to
31 inclusive days; today stops at the latest completed minute. Historical ranges
before listing, missing candles and conflicting duplicates fail explicitly.

Choose the assumed intraminute path O-H-L-C or O-L-H-C. Level crossings are
interpolated within that synthetic path; the 30-second SL cooldown uses simulated
time, never the production clock. Both variants are scenarios, not guaranteed
upper/lower profit bounds. There is no reconstructed tick history, so results
cannot be expected to equal real-time paper activity. References use only candles
completed before the simulated tick. There is no daily or forced range-end exit.

Reports include an equity chart, closed trades, any range-end open position,
reference timestamps, SL/target/breakeven facts, lots, margin estimates, INR and
CSV containing settings and assumptions. Product fees/size/margin metadata are
current, not a historical schedule. No funding, tax, slippage or liquidation model.
The replay makes no database writes or private exchange requests. One replay runs
at a time per worker. Neither paper settings nor active positions are changed.

## Feed recovery

The worker runs independently of FYERS login, trading mode, market hours and the
browser being open. WS trades are primary; REST recent trades provide fallback.
Only fresh exchange timestamps can drive entries/exits; old or duplicate samples
are rejected. A socket handshake alone does not mean a subscription succeeded.

Completed reference candles come from Delta history, not from a sparse local
tick reconstruction. Missing recent candles block entries and are retried, while
fresh-price exits remain active. No trades are backdated to missed market moves
during a backend/feed outage. A thin market may legitimately have no fresh trade.
No API can recover an unobserved intrabar path with certainty.

## Verification

```powershell
cd backend
python -m tests.smoke_delta_paper
python -m tests.smoke_delta_reporting
python -m tests.smoke_delta_controls
python -m tests.smoke_delta_backtest
python -m tests.smoke_silver_logic
python -m tests.smoke_silver_micro_2
python -m tests.smoke_silver_v_micro
python -m tests.smoke_live_orders
```

Official references:
- https://docs-global.delta.exchange/
- https://docs.delta.exchange/
- https://api.india.delta.exchange/v2/products/PAXGUSD

Public product checks on 2026-09-09 confirmed India `PAXGUSD` is live.
Earlier Global checks confirmed `PAXGUSD` is invalid on Global and `PAXGUSDT`
is live there; the Global history/WS checks do not verify India's feed.
These public checks do not verify your Railway-to-VM route, private API key,
Supabase migration or deployed instance; verify those after configuring them.
