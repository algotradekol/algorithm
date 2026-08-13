'use client';

import { useEffect, useState } from 'react';

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
  const [displayMode, setDisplayMode] = useState<TradingMode>(normalizeMode(mode));

  useEffect(() => {
    setDisplayMode(normalizeMode(mode));
  }, [mode]);

  const currentMode = displayMode;

  async function switchMode(nextMode: TradingMode) {
    if (loading || nextMode === currentMode) {
      return;
    }

    // Extra warning during Indian market hours (09:05-15:30 IST). Each toggle
    // during market hours burns Fyers WS handshake quota — on 2026-08-13 the
    // user toggled 5 times between 09:11 and 09:17 and it contributed to
    // 429s that killed 9:15 trade execution.
    const now = new Date();
    const istOffsetMin = 5 * 60 + 30;
    const utcMin = now.getUTCHours() * 60 + now.getUTCMinutes();
    const istMin = (utcMin + istOffsetMin) % (24 * 60);
    const inMarketHours = istMin >= (9 * 60 + 5) && istMin < (15 * 60 + 30);

    let confirmMessage: string;
    if (inMarketHours) {
      confirmMessage =
        `⚠️  Market hours (09:05-15:30 IST) — switching mode NOW burns Fyers WS quota and can 429 your live trades.\n\n` +
        `Also: mode toggles have a 30-second cooldown — a second toggle within 30s will be rejected.\n\n` +
        `Switch to ${nextMode.toUpperCase()} anyway?`;
    } else {
      confirmMessage = `Switch trading mode to ${nextMode.toUpperCase()}? Open positions stay preserved in their current mode and the other mode gets its own broker state.`;
    }
    const confirmed = window.confirm(confirmMessage);
    if (!confirmed) {
      return;
    }

    setLoading(true);
    setDisplayMode(nextMode);
    api.clearFyersAccountCache();
    try {
      const response = await api.updateTradingMode(nextMode) as { trading_mode?: TradingMode; warning?: string };
      const activeMode = normalizeMode(response.trading_mode ?? nextMode);
      api.clearFyersAccountCache();
      setDisplayMode(activeMode);
      onModeChanged?.(activeMode);
      if (response.warning) {
        window.setTimeout(() => window.alert(response.warning as string), 50);
      }
    } catch (error) {
      setDisplayMode(currentMode);
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
