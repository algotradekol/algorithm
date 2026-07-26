'use client';

import { useState } from 'react';

import { api } from '../lib/api';

type TradingMode = 'paper' | 'live';

function normalizeMode(value?: string | null): TradingMode {
  return value === 'live' ? 'live' : 'paper';
}

export default function TradingModeToggle({
  mode,
  onModeChanged,
}: {
  mode?: string | null;
  onModeChanged?: (mode: TradingMode) => void;
}) {
  const [loading, setLoading] = useState(false);
  const currentMode = normalizeMode(mode);

  async function switchMode(nextMode: TradingMode) {
    if (loading || nextMode === currentMode) {
      return;
    }
    const confirmed = window.confirm(
      `Switch trading mode to ${nextMode.toUpperCase()}? Make sure there are no open positions before changing modes.`,
    );
    if (!confirmed) {
      return;
    }

    setLoading(true);
    try {
      const response = await api.updateTradingMode(nextMode) as { trading_mode?: TradingMode };
      const activeMode = normalizeMode(response.trading_mode ?? nextMode);
      onModeChanged?.(activeMode);
      window.location.reload();
    } catch (error) {
      alert(error instanceof Error ? error.message : 'Unable to switch trading mode');
    } finally {
      setLoading(false);
    }
  }

  const paperActive = currentMode === 'paper';
  const liveActive = currentMode === 'live';

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs uppercase tracking-wider text-gray-500">Mode</span>
      <div className="inline-flex min-h-10 overflow-hidden rounded-full border border-[#1f2937] bg-[#0f172a] p-1">
        <button
          type="button"
          onClick={() => switchMode('paper')}
          disabled={loading}
          className={`min-w-20 rounded-full px-3 py-1.5 text-xs font-semibold uppercase tracking-wider transition ${
            paperActive ? 'bg-[#3b82f6] text-white' : 'text-gray-500 hover:text-gray-100'
          } ${loading ? 'cursor-wait opacity-70' : ''}`}
        >
          Paper
        </button>
        <button
          type="button"
          onClick={() => switchMode('live')}
          disabled={loading}
          className={`min-w-20 rounded-full px-3 py-1.5 text-xs font-semibold uppercase tracking-wider transition ${
            liveActive ? 'bg-[#22c55e] text-white' : 'text-gray-500 hover:text-gray-100'
          } ${loading ? 'cursor-wait opacity-70' : ''}`}
        >
          Live
        </button>
      </div>
    </div>
  );
}

