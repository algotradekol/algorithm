import { supabase } from './supabaseClient';

const API_URL = process.env.NEXT_PUBLIC_API_URL;

async function authedFetch(path: string, options: RequestInit = {}) {
  if (!API_URL) throw new Error('NEXT_PUBLIC_API_URL is not configured');
  const { data: { session } } = await supabase.auth.getSession();
  if (!session) throw new Error('Not logged in');

  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      ...options.headers,
      Authorization: `Bearer ${session.access_token}`,
      'Content-Type': 'application/json',
    },
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
  return res.json();
}

export const api = {
  summary: (algoId: string) => authedFetch(`/api/algo/${algoId}/summary`),
  positions: (algoId: string) => authedFetch(`/api/algo/${algoId}/positions`),
  trades: (algoId: string) => authedFetch(`/api/algo/${algoId}/trades`),
  history: (algoId: string, days = 30) => authedFetch(`/api/algo/${algoId}/history?days=${days}`),
  compare: () => authedFetch('/api/compare'),
  getCharges: () => authedFetch('/api/charges'),
  updateCharges: (config: object) =>
    authedFetch('/api/charges', { method: 'PUT', body: JSON.stringify(config) }),
  watchlist: () => authedFetch('/api/watchlist'),
  marketHistory: (symbol: string, days = 5, resolution = '15') =>
    authedFetch(`/api/market/history?symbol=${encodeURIComponent(symbol)}&days=${days}&resolution=${encodeURIComponent(resolution)}`),
};
