'use client';

import { useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { getAuthToken } from '../lib/authToken';

const API_URL = process.env.NEXT_PUBLIC_API_URL;

export default function FyersLoginButton({ connected = false }: { connected?: boolean }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const searchParams = useSearchParams();
  const connectedFromRedirect = searchParams.get('fyers_login') === 'success';

  async function handleClick() {
    if (!API_URL) {
      setError('NEXT_PUBLIC_API_URL is not configured');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const token = await getAuthToken();
      if (!token) throw new Error('Not logged in');

      const res = await fetch(`${API_URL}/api/fyers/login-url`, {
        headers: {
          Authorization: `Bearer ${token}`,
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

  if (connected || connectedFromRedirect) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-300">
        <i className="ri-shield-check-fill text-sm text-[#22c55e]" />
        Fyers Connected
      </div>
    );
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={handleClick}
        disabled={loading}
        className={`inline-flex min-h-10 items-center gap-2 rounded border bg-transparent px-3 py-1.5 text-sm font-medium transition hover:bg-[#3b82f6] hover:text-white disabled:cursor-wait ${
          loading ? 'border-[#f59e0b] text-[#f59e0b]' : 'border-[#3b82f6] text-[#3b82f6]'
        }`}
      >
        <i className={`${loading ? 'ri-error-warning-fill text-[#f59e0b]' : 'ri-login-circle-fill text-[#3b82f6]'} text-sm`} />
        {loading ? 'Connecting...' : 'Login to Fyers'}
      </button>
      {error && <p className="m-0 max-w-xs text-right text-xs text-[#ef4444]">{error}</p>}
    </div>
  );
}
