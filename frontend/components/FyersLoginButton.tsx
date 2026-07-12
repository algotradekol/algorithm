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
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 8 }}>
      <button
        onClick={handleClick}
        disabled={loading}
        style={{
          background: '#0f6f44',
          border: '1px solid #198754',
          color: '#fff',
          padding: '6px 12px',
          borderRadius: 6,
          cursor: loading ? 'wait' : 'pointer',
          opacity: loading ? 0.8 : 1,
        }}
      >
        {loading ? 'Opening Fyers...' : 'Login to Fyers'}
      </button>
      {error && <p style={{ margin: 0, color: '#ff6b6b', fontSize: 12 }}>{error}</p>}
    </div>
  );
}
