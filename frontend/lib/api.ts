import { getAuthToken } from './authToken';
import { clearPinToken } from './pinAuth';
import { supabase } from './supabaseClient';

const API_URL = process.env.NEXT_PUBLIC_API_URL;
type TradingMode = 'paper' | 'live';
const fyersPositionsInFlight = new Map<TradingMode, Promise<any>>();
const fyersPositionsCache = new Map<TradingMode, { value: any; cachedAt: number }>();
const fyersOrdersInFlight = new Map<TradingMode, Promise<any>>();
const fyersOrdersCache = new Map<TradingMode, { value: any; cachedAt: number }>();
const fyersFundsInFlight = new Map<TradingMode, Promise<any>>();
const fyersFundsCache = new Map<TradingMode, { value: any; cachedAt: number }>();
const FYERS_POSITIONS_CLIENT_CACHE_MS = 8_000;
const FYERS_ORDERS_CLIENT_CACHE_MS = 8_000;
const FYERS_FUNDS_CLIENT_CACHE_MS = 15_000;

async function authedFetch(path: string, options: RequestInit = {}) {
  if (!API_URL) throw new Error('NEXT_PUBLIC_API_URL is not configured');
  const token = await getAuthToken();
  if (!token) throw new Error('Not logged in');

  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      ...options.headers,
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
  });
  if (!res.ok) {
    if (res.status === 401 && typeof window !== 'undefined') {
      // Do not keep a stale dashboard alive after an email/PIN token expires.
      clearPinToken();
      void supabase.auth.signOut();
      window.dispatchEvent(new Event('algo-auth-expired'));
    }
    const body = await res.text();
    let message = body;
    try {
      const parsed = JSON.parse(body);
      message = typeof parsed.detail === 'string'
        ? parsed.detail
        : parsed.detail?.message || body;
    } catch {
      // Keep the raw response body if it is not JSON.
    }
    throw new Error(`API error ${res.status}: ${message}`);
  }
  return res.json();
}

function assertAccountMode(value: any, mode: TradingMode) {
  if (value?.trading_mode && value.trading_mode !== mode) {
    throw new Error(`Discarded stale ${value.trading_mode} broker response while ${mode} mode is active`);
  }
  return value;
}

function clearFyersAccountCache() {
  fyersPositionsCache.clear();
  fyersPositionsInFlight.clear();
  fyersOrdersCache.clear();
  fyersOrdersInFlight.clear();
  fyersFundsCache.clear();
  fyersFundsInFlight.clear();
}

function fetchFyersPositions(mode: TradingMode = 'live', force = false) {
  const now = Date.now();
  const cached = fyersPositionsCache.get(mode);
  if (!force && cached && now - cached.cachedAt < FYERS_POSITIONS_CLIENT_CACHE_MS) {
    return Promise.resolve(cached.value);
  }
  const inFlight = fyersPositionsInFlight.get(mode);
  if (inFlight) return inFlight;

  const request = authedFetch(`/api/fyers/positions?mode=${mode}`)
    .then((value) => {
      const checked = assertAccountMode(value, mode);
      fyersPositionsCache.set(mode, { value: checked, cachedAt: Date.now() });
      return checked;
    })
    .finally(() => {
      fyersPositionsInFlight.delete(mode);
    });
  fyersPositionsInFlight.set(mode, request);
  return request;
}

function fetchFyersFunds(mode: TradingMode = 'live', force = false) {
  const now = Date.now();
  const cached = fyersFundsCache.get(mode);
  if (!force && cached && now - cached.cachedAt < FYERS_FUNDS_CLIENT_CACHE_MS) {
    return Promise.resolve(cached.value);
  }
  const inFlight = fyersFundsInFlight.get(mode);
  if (inFlight) return inFlight;

  const request = authedFetch(`/api/fyers/funds?mode=${mode}`)
    .then((value) => {
      const checked = assertAccountMode(value, mode);
      fyersFundsCache.set(mode, { value: checked, cachedAt: Date.now() });
      return checked;
    })
    .finally(() => {
      fyersFundsInFlight.delete(mode);
    });
  fyersFundsInFlight.set(mode, request);
  return request;
}

function fetchFyersOrders(mode: TradingMode = 'live', force = false) {
  const now = Date.now();
  const cached = fyersOrdersCache.get(mode);
  if (!force && cached && now - cached.cachedAt < FYERS_ORDERS_CLIENT_CACHE_MS) {
    return Promise.resolve(cached.value);
  }
  const inFlight = fyersOrdersInFlight.get(mode);
  if (inFlight) return inFlight;

  const request = authedFetch(`/api/fyers/orders?mode=${mode}`)
    .then((value) => {
      const checked = assertAccountMode(value, mode);
      fyersOrdersCache.set(mode, { value: checked, cachedAt: Date.now() });
      return checked;
    })
    .finally(() => {
      fyersOrdersInFlight.delete(mode);
    });
  fyersOrdersInFlight.set(mode, request);
  return request;
}

export const api = {
  summary: (algoId: string) => authedFetch(`/api/algo/${algoId}/summary`),
  positions: (algoId: string) => authedFetch(`/api/algo/${algoId}/positions`),
  exitPosition: (algoId: string, positionId: string) =>
    authedFetch(`/api/algo/${algoId}/positions/${encodeURIComponent(positionId)}/exit`, { method: 'POST' }),
  manualTrade: (algoId: string, payload: { symbol: string; side: 'BUY' | 'SELL'; price?: number; trigger?: string }) =>
    authedFetch(`/api/algo/${algoId}/manual-trade`, { method: 'POST', body: JSON.stringify(payload) }),
  trades: (algoId: string) => authedFetch(`/api/algo/${algoId}/trades`),
  history: (algoId: string, days = 30) => authedFetch(`/api/algo/${algoId}/history?days=${days}`),
  scanResults: (algoId: string) => authedFetch(`/api/algo/${algoId}/scan-results`),
  feedStatus: (algoId: string) => authedFetch(`/api/algo/${algoId}/feed-status`),
  getSettings: (algoId: string) => authedFetch(`/api/algo/${algoId}/settings`),
  updateSettings: (algoId: string, settings: object) =>
    authedFetch(`/api/algo/${algoId}/settings`, { method: 'PUT', body: JSON.stringify(settings) }),
  updateAvailableCash: (algoId: string, cash: number) =>
    authedFetch(`/api/algo/${algoId}/available-cash`, { method: 'PUT', body: JSON.stringify({ cash }) }),
  resetSettings: (algoId: string) =>
    authedFetch(`/api/algo/${algoId}/settings/reset`, { method: 'POST' }),
  compare: () => authedFetch('/api/compare'),
  calendarDays: (days = 60) => authedFetch(`/api/calendar?days=${days}`),
  calendarDay: (date: string) => authedFetch(`/api/calendar/${encodeURIComponent(date)}`),
  deleteCalendarDay: (date: string) => authedFetch(`/api/calendar/${encodeURIComponent(date)}`, { method: 'DELETE' }),
  deleteCalendarSnapshot: (date: string, algoId: string) =>
    authedFetch(`/api/calendar/${encodeURIComponent(date)}/${encodeURIComponent(algoId)}`, { method: 'DELETE' }),
  saveCalendarSnapshot: (payload: object = {}) =>
    authedFetch('/api/calendar/snapshot', { method: 'POST', body: JSON.stringify(payload) }),
  engineStatus: () => authedFetch('/api/engine/status'),
  fyersStatus: () => authedFetch('/api/fyers/status'),
  tradingMode: () => authedFetch('/api/runtime/trading-mode'),
  updateTradingMode: (mode: 'paper' | 'live') =>
    authedFetch('/api/runtime/trading-mode', { method: 'PUT', body: JSON.stringify({ trading_mode: mode }) }),
  fyersRefreshToken: () => authedFetch('/api/fyers/refresh-token', { method: 'POST' }),
  fyersDisconnect: () => authedFetch('/api/fyers/disconnect', { method: 'POST' }),
  fyersTokenStatus: () => authedFetch('/api/fyers/token-status'),
  fyersFunds: fetchFyersFunds,
  fyersPositions: fetchFyersPositions,
  fyersOrders: fetchFyersOrders,
  clearFyersAccountCache,
  aiSessions: () => authedFetch('/api/ai/sessions'),
  aiCreateSession: (title = 'New chat') => authedFetch('/api/ai/sessions', { method: 'POST', body: JSON.stringify({ title }) }),
  aiMessages: (sessionId: string) => authedFetch(`/api/ai/sessions/${sessionId}/messages`),
  aiDeleteSession: (sessionId: string) => authedFetch(`/api/ai/sessions/${sessionId}`, { method: 'DELETE' }),
  aiChat: (payload: object) => authedFetch('/api/ai/chat', { method: 'POST', body: JSON.stringify(payload) }),
  getCharges: () => authedFetch('/api/charges'),
  updateCharges: (config: object) =>
    authedFetch('/api/charges', { method: 'PUT', body: JSON.stringify(config) }),
  watchlist: () => authedFetch('/api/watchlist'),
  marketHistory: (symbol: string, days = 5, resolution = '15') =>
    authedFetch(`/api/market/history?symbol=${encodeURIComponent(symbol)}&days=${days}&resolution=${encodeURIComponent(resolution)}`),
  startBacktest: (payload: { algo_id: string; start_date: string; end_date: string }) =>
    authedFetch('/api/backtests', { method: 'POST', body: JSON.stringify(payload) }),
  backtestStatus: (jobId: string) => authedFetch(`/api/backtests/${encodeURIComponent(jobId)}`),
  cancelBacktest: (jobId: string) =>
    authedFetch(`/api/backtests/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
};
