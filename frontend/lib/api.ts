import { supabase } from './supabaseClient';

const API_URL = process.env.NEXT_PUBLIC_API_URL;

async function authedFetch(path: string, options: RequestInit = {}) {
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
  compare: () => authedFetch('/api/compare'),
  getCharges: () => authedFetch('/api/charges'),
  updateCharges: (config: object) =>
    authedFetch('/api/charges', { method: 'PUT', body: JSON.stringify(config) }),
  watchlist: () => authedFetch('/api/watchlist'),
};
