import { getAuthToken } from './authToken';

const API_URL = process.env.NEXT_PUBLIC_API_URL;

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

export const api = {
  summary: (algoId: string) => authedFetch(`/api/algo/${algoId}/summary`),
  positions: (algoId: string) => authedFetch(`/api/algo/${algoId}/positions`),
  trades: (algoId: string) => authedFetch(`/api/algo/${algoId}/trades`),
  history: (algoId: string, days = 30) => authedFetch(`/api/algo/${algoId}/history?days=${days}`),
  scanResults: (algoId: string) => authedFetch(`/api/algo/${algoId}/scan-results`),
  getSettings: (algoId: string) => authedFetch(`/api/algo/${algoId}/settings`),
  updateSettings: (algoId: string, settings: object) =>
    authedFetch(`/api/algo/${algoId}/settings`, { method: 'PUT', body: JSON.stringify(settings) }),
  resetSettings: (algoId: string) =>
    authedFetch(`/api/algo/${algoId}/settings/reset`, { method: 'POST' }),
  compare: () => authedFetch('/api/compare'),
  calendarDays: (days = 60) => authedFetch(`/api/calendar?days=${days}`),
  calendarDay: (date: string) => authedFetch(`/api/calendar/${encodeURIComponent(date)}`),
  saveCalendarSnapshot: (payload: object = {}) =>
    authedFetch('/api/calendar/snapshot', { method: 'POST', body: JSON.stringify(payload) }),
  engineStatus: () => authedFetch('/api/engine/status'),
  fyersStatus: () => authedFetch('/api/fyers/status'),
  fyersRefreshToken: () => authedFetch('/api/fyers/refresh-token', { method: 'POST' }),
  fyersTokenStatus: () => authedFetch('/api/fyers/token-status'),
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
};
