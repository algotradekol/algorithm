'use client';

import { useState } from 'react';
import { supabase } from '../lib/supabaseClient';

const API_URL = process.env.NEXT_PUBLIC_API_URL;

export default function FyersLoginButton() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function handleClick() {
    if (!API_URL) {
      setError('NEXT_PUBLIC_API_URL is not configured');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const { data: { session } } = await supabase.auth.getSession();
      if (!session) throw new Error('Not logged in');

      const res = await fetch(`${API_URL}/api/fyers/login-url`, {
        headers: {
          Authorization: `Bearer ${session.access_token}`,
        },
      });

      if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);

      const data = await res.json() as { url?: string };
      if (!data.url) throw new Error('Fyers login URL was not returned');

      window.open(data.url, '_blank');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start Fyers login');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-2">
      <button
        onClick={handleClick}
        disabled={loading}
        className="rounded-md border border-success/70 bg-action px-3 py-2 text-sm font-semibold text-white transition hover:bg-success hover:text-ink disabled:cursor-wait disabled:opacity-80"
      >
        {loading ? 'Opening Fyers...' : 'Login to Fyers'}
      </button>
      {error && <p className="m-0 max-w-xs text-right text-xs text-danger">{error}</p>}
    </div>
  );
}
