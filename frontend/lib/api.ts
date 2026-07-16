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
  compare: () => authedFetch('/api/compare'),
  engineStatus: () => authedFetch('/api/engine/status'),
  fyersStatus: () => authedFetch('/api/fyers/status'),
  getCharges: () => authedFetch('/api/charges'),
  updateCharges: (config: object) =>
    authedFetch('/api/charges', { method: 'PUT', body: JSON.stringify(config) }),
  watchlist: () => authedFetch('/api/watchlist'),
  marketHistory: (symbol: string, days = 5, resolution = '15') =>
    authedFetch(`/api/market/history?symbol=${encodeURIComponent(symbol)}&days=${days}&resolution=${encodeURIComponent(resolution)}`),
};
