'use client';

import { useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { getAuthToken } from '../lib/authToken';

const API_URL = process.env.NEXT_PUBLIC_API_URL;

export default function FyersLoginButton({
  connected = false,
  mode = 'paper',
  autoRecovering = false,
  sessionState,
}: {
  connected?: boolean;
  mode?: 'paper' | 'live';
  autoRecovering?: boolean;
  sessionState?: string;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const searchParams = useSearchParams();
  const connectedFromRedirect = searchParams.get('fyers_login') === 'success';
  const isVerifyingSession = sessionState === 'token_present_settling';
  const isRecoveringFeed = sessionState === 'token_present_ws_recovering';

  async function handleClick() {
    if (autoRecovering) {
      // F14: block the user from initiating a new OAuth exchange while
      // the backend is auto-reconnecting. On 2026-08-17 a re-login mid
      // recovery invalidated the healing session and triggered the
      // Cloudflare rate-limit cascade.
      setError('Auto-reconnecting. Wait a moment before logging in again.');
      return;
    }
    if (!API_URL) {
      setError('NEXT_PUBLIC_API_URL is not configured');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const token = await getAuthToken();
      if (!token) throw new Error('Not logged in');

      const res = await fetch(`${API_URL}/api/fyers/login-url?mode=${encodeURIComponent(mode)}`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);

      const data = await res.json() as { url?: string };
      if (!data.url) throw new Error('Fyers login URL was not returned');

      window.location.assign(data.url);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start Fyers login');
    } finally {
      setLoading(false);
    }
  }

  // Confirmed connected for the CURRENT mode.
  if (connected) {
    return (
      <div
        className="flex items-center gap-2 text-sm text-gray-300"
        title={`Fyers ${mode.toUpperCase()} session is active. Toggle mode to switch accounts.`}
      >
        <i className="ri-shield-check-fill text-sm text-[#22c55e]" />
        Fyers Connected <span className="text-[10px] text-gray-500">({mode.toUpperCase()})</span>
      </div>
    );
  }

  // Just returned from OAuth callback; backend status hasn't refreshed yet.
  // Show a transient "Verifying" state instead of falsely claiming connected
  // — this was the 2026-08-18 bug: users on PAPER mode saw "Fyers Connected"
  // for a LIVE-mode redirect even though no PAPER token existed.
  if (connectedFromRedirect || isVerifyingSession || isRecoveringFeed) {
    return (
      <div
        className="flex items-center gap-2 text-sm text-[#f59e0b]"
        title={
          isRecoveringFeed
            ? 'Fyers token is present and the live feed is reconnecting.'
            : 'Fyers token is present and the backend is verifying the session.'
        }
      >
        <i className="ri-refresh-line text-sm text-[#f59e0b] animate-spin" />
        {isRecoveringFeed ? 'Reconnecting Fyers feed…' : 'Verifying Fyers session…'}
      </div>
    );
  }

  const disabled = loading || autoRecovering;
  const label = autoRecovering
    ? 'Reconnecting…'
    : loading
      ? 'Connecting...'
      : `Login to Fyers (${mode.toUpperCase()})`;
  const borderColor = autoRecovering || loading ? 'border-[#f59e0b] text-[#f59e0b]' : 'border-[#3b82f6] text-[#3b82f6]';
  const icon = autoRecovering
    ? 'ri-refresh-line text-[#f59e0b] animate-spin'
    : loading
      ? 'ri-error-warning-fill text-[#f59e0b]'
      : 'ri-login-circle-fill text-[#3b82f6]';
  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={handleClick}
        disabled={disabled}
        className={`inline-flex min-h-10 items-center gap-2 rounded border bg-transparent px-3 py-1.5 text-sm font-medium transition hover:bg-[#3b82f6] hover:text-white disabled:cursor-wait ${borderColor}`}
        title={autoRecovering ? 'System is reconnecting to Fyers. No action needed.' : undefined}
      >
        <i className={`${icon} text-sm`} />
        {label}
      </button>
      {error && <p className="m-0 max-w-xs text-right text-xs text-[#ef4444]">{error}</p>}
    </div>
  );
}
