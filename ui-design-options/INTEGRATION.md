# AlgoDesk frontend — backend integration notes

This prototype (`vela-algodesk-app.html`) is a **single self-contained HTML file** that already
speaks your FastAPI backend's contract. It renders from built-in **demo data** when no backend is
configured, and switches to **live data** the moment a base URL + token are set.

## How to connect it (no code needed to try)
Click the **DEMO DATA** badge (top-right) → enter your API base URL (e.g. the Railway/Render URL of
`backend/app/main.py`) and a bearer token (a Supabase session `access_token`, or a PIN token from
`POST /api/pin-login`) → **Connect**. Everything reloads live. Blank = demo.

The base URL + token persist in `localStorage` (`algodesk_base`, `algodesk_token`).

## Where the wiring lives
All of it is in one `<script>` at the bottom of the file:

- **`CONFIG`** — base URL + token (from localStorage).
- **`authedFetch(path, opts)`** — adds `Authorization: Bearer` + JSON headers; throws on non-2xx
  (mirrors `frontend/lib/api.ts`).
- **`API`** — one method per backend route (all ~45). Names/paths/verbs match `main.py` exactly.
- **`DEMO`** — demo responses, shaped to match each endpoint (same field names as the real broker
  summary, positions, trades, scan results, settings defaults, charges, backtest job, etc).
- **`fetchOr(demoValue, () => API.x())`** — the live-or-demo switch. Live when `CONFIG.base` is set,
  falls back to demo on any error, and sets the `SOURCE` badge.
- **`render*()`** — build each page from whatever `fetchOr` returned. The **same render code** runs
  for demo and live, so wiring is already done — only the data source changes.

## Endpoints covered (method → API method → render/consumer)
| Backend route | `API.*` | Used by |
|---|---|---|
| `GET /api/engine/status` | `engineStatus` | topbar chips, diagnostics strip, Feed Health |
| `GET /api/fyers/status` | `fyersStatus` | topbar LIVE/Fyers state |
| `GET|PUT /api/runtime/trading-mode` | `tradingMode` / `updateTradingMode` | Paper/Live toggle |
| `GET /api/algo/{id}/summary` | `summary` | KPI tiles, sidebar counts |
| `GET /api/algo/{id}/positions` | `positions` | Open Positions table + Exit |
| `POST /api/algo/{id}/positions/{pid}/exit` | `exitPosition` | per-row **Exit** + **Exit all** |
| `POST /api/algo/{id}/manual-trade` | `manualTrade` | (ready for a manual-order form) |
| `GET /api/algo/{id}/trades` | `trades` | Closed Trades Today |
| `GET /api/algo/{id}/history` | `history` | History → Daily Performance |
| `GET /api/algo/{id}/setup-history` | `setupHistory` | Silver setup history |
| `GET /api/algo/{id}/scan-results` | `scanResults` | Signal Funnel + Scan Results |
| `GET|PUT /api/algo/{id}/settings` | `getSettings` / `updateSettings` | Settings drawer (Save) |
| `PUT /api/algo/{id}/available-cash` | `updateAvailableCash` | Settings → Available Cash |
| `POST /api/algo/{id}/settings/reset` | `resetSettingsApi` | Settings → Reset |
| `POST /api/algo/{id}/scan-enabled` | `setScanEnabled` | Scanning on/off button |
| `GET /api/compare` | `compare` | Compare table |
| `GET /api/calendar` (+ day/snapshot/delete) | `calendarDays` … | Calendar heat + Save snapshot |
| `GET|PUT /api/charges` | `getCharges` / `updateCharges` | Charges page (Save) |
| `GET /api/watchlist` | `watchlist` | (symbol universe) |
| `GET /api/market/history` | `marketHistory` | History candlestick chart |
| `POST /api/backtests` + `GET /api/backtests/{job}` | `startBacktest` / `backtestStatus` | Backtest run + poll |
| `GET /api/ai/sessions`, `POST /api/ai/chat` (+ session CRUD) | `aiSessions` / `aiChat` … | AI Copilot |
| `GET /api/fyers/{funds,positions,orders,login-url,token-status}`, refresh/disconnect | `fyers*` | (ready to surface) |

## Recommended path to production
This file is the **design + contract reference**. To ship, port the same structure into your Next.js
app under `frontend/`:
1. The `API` object here maps 1:1 onto your existing `frontend/lib/api.ts` — reuse that instead.
2. Each `render*()` becomes a React component; the demo objects become loading/empty fallbacks.
3. Wire the live WebSocket (`/ws`) for tick/position-closed events to replace the 10s poll.
4. Keep the exact settings keys (they match `strategy_settings.py`) so PUT payloads round-trip.

Auth, WebSocket streaming, and Supabase session handling already exist in your codebase — this
prototype deliberately keeps those as simple bearer-token fetches so the UI is easy to explore.
