'use client';
import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { supabase } from '../../lib/supabaseClient';
import AlgoTab from '../../components/AlgoTab';
import CompareTab from '../../components/CompareTab';
import ChargesPanel from '../../components/ChargesPanel';
import HistoryTab from '../../components/HistoryTab';
import FyersLoginButton from '../../components/FyersLoginButton';
import { getAuthToken } from '../../lib/authToken';
import { clearPinToken } from '../../lib/pinAuth';
import { api } from '../../lib/api';

const TABS = ['Algo 1', 'Algo 2', 'Algo 3', 'Algo 4', 'Compare', 'History', 'Charges'] as const;

function formatIstTime() {
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date());
}

function DashboardContent() {
  const [tab, setTab] = useState<(typeof TABS)[number]>('Algo 1');
  const [ready, setReady] = useState(false);
  const [showFyersBanner, setShowFyersBanner] = useState(true);
  const [istTime, setIstTime] = useState(formatIstTime());
  const [fyersStatus, setFyersStatus] = useState<{
    connected: boolean;
    status: string;
    message: string;
  } | null>(null);
  const [engineStatus, setEngineStatus] = useState<{
    state: string;
    error?: string | null;
    watchlist_count: number;
    strategies_running: string[];
  } | null>(null);
  const router = useRouter();
  const searchParams = useSearchParams();
  const fyersLogin = searchParams.get('fyers_login');
  const tradingReady = Boolean(fyersStatus?.connected && engineStatus?.state === 'running');
  const statusTone = fyersStatus?.connected ? 'bg-[#22c55e]' : fyersStatus?.status === 'disconnected' ? 'bg-[#f59e0b]' : 'bg-[#ef4444]';
  const statusText = fyersStatus?.connected ? 'LIVE' : fyersStatus?.status === 'disconnected' ? 'TOKEN MISSING' : 'STOPPED';

  useEffect(() => {
    getAuthToken().then((token) => {
      if (!token) router.replace('/login');
      else setReady(true);
    });
  }, [router]);

  useEffect(() => {
    const interval = window.setInterval(() => setIstTime(formatIstTime()), 1000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    if (!ready) return;

    let cancelled = false;
    async function loadFyersStatus() {
      try {
        const status = await api.fyersStatus();
        if (!cancelled) setFyersStatus(status);
      } catch (error) {
        if (!cancelled) {
          setFyersStatus({
            connected: false,
            status: 'error',
            message: error instanceof Error ? error.message : 'Unable to check Fyers status',
          });
        }
      }
    }

    async function loadEngineStatus() {
      try {
        const status = await api.engineStatus();
        if (!cancelled) setEngineStatus(status);
      } catch {
        if (!cancelled) setEngineStatus(null);
      }
    }

    loadFyersStatus();
    loadEngineStatus();
    const interval = window.setInterval(() => {
      loadFyersStatus();
      loadEngineStatus();
    }, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [ready, fyersLogin]);

  if (!ready) return null;

  return (
    <main className="min-h-screen bg-[#0a0e14]">
      <div className="mx-auto max-w-[1400px] px-6 py-4">
        {fyersLogin && showFyersBanner && (
          <div
            className={`mb-3 flex items-center justify-between gap-3 rounded border px-3 py-2 ${
              fyersLogin === 'success'
                ? 'border-[#22c55e]/40 bg-[#22c55e]/10'
                : 'border-[#ef4444]/40 bg-[#ef4444]/10'
            }`}
          >
            <span className="text-sm text-gray-100">
              {fyersLogin === 'success' ? 'Fyers login successful' : 'Fyers login failed, try again'}
            </span>
            <button
              onClick={() => setShowFyersBanner(false)}
              className="text-xs uppercase tracking-wider text-gray-500 hover:text-gray-100"
            >
              Dismiss
            </button>
          </div>
        )}

        <header className="flex flex-col gap-3 border-b border-[#1f2937] pb-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <div className="font-mono text-base font-semibold tracking-[0.18em] text-gray-100">ALGO TRADING</div>
            <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-gray-400">
              <span className={`h-2 w-2 rounded-full ${statusTone}`} />
              <span>{statusText}</span>
            </div>
            <div className="font-mono text-sm tabular-nums text-gray-300">{istTime} IST</div>
            <div
              title={engineStatus?.error || `${engineStatus?.watchlist_count || 0} symbols loaded`}
              className="flex items-center gap-2 text-xs uppercase tracking-wider text-gray-400"
            >
              <span className={`h-2 w-2 rounded-full ${engineStatus?.state === 'running' ? 'bg-[#22c55e]' : 'bg-[#f59e0b]'}`} />
              <span>Engine {engineStatus?.state || 'checking'}</span>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <FyersLoginButton />
            <button
              onClick={async () => { clearPinToken(); await supabase.auth.signOut(); router.replace('/login'); }}
              className="text-sm text-gray-500 hover:text-gray-100"
            >
              Logout
            </button>
          </div>
        </header>

        <nav className="mb-4 flex gap-6 overflow-x-auto border-b border-[#1f2937]">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`whitespace-nowrap border-b-2 px-0 py-3 text-sm font-medium ${
                tab === t
                  ? 'border-[#3b82f6] text-gray-100'
                  : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              {t}
            </button>
          ))}
        </nav>

        {!tradingReady && tab !== 'Charges' ? (
          <section className="panel p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-gray-100">
              <span className={`h-2 w-2 rounded-full ${statusTone}`} />
              Connect Fyers to start polling
            </div>
            <p className="mt-2 text-sm text-gray-500">
              The dashboard will not call algo summary, positions, trades, watchlist, or market-history endpoints until
              Fyers is connected and the trading engine is running.
            </p>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <StatusCard
                label="Fyers"
                dotClass={statusTone}
                value={fyersStatus ? (fyersStatus.connected ? 'Connected' : fyersStatus.status) : 'Checking'}
                detail={fyersStatus?.message || 'Waiting for broker status check.'}
              />
              <StatusCard
                label="Engine"
                dotClass={engineStatus?.state === 'running' ? 'bg-[#22c55e]' : 'bg-[#f59e0b]'}
                value={engineStatus?.state || 'Checking'}
                detail={engineStatus?.error || `${engineStatus?.watchlist_count || 0} symbols loaded.`}
              />
            </div>
          </section>
        ) : (
          <>
            {tab === 'Algo 1' && <AlgoTab algoId="algo1" displayName="Algo 1 - Opening Range Gap" />}
            {tab === 'Algo 2' && <AlgoTab algoId="algo2" displayName="Algo 2 - VWAP/EMA/Volume Momentum" />}
            {tab === 'Algo 3' && (
              <AlgoTab
                algoId="algo3"
                displayName="Algo 3 - Opening Range Gap (Basic)"
                description="9:15 candle open=low/high + 0.5-2% gap filter. No indicator filters. Max 10 trades (5B+5S)."
              />
            )}
            {tab === 'Algo 4' && (
              <AlgoTab
                algoId="algo4"
                displayName="Algo 4 - Opening Range Gap (With Indicators)"
                description="Same as Algo 3 + VWAP, EMA20/EMA50, RSI, ADX, Supertrend, volume and liquidity filters. Fewer but higher-quality signals."
              />
            )}
            {tab === 'Compare' && <CompareTab />}
            {tab === 'History' && <HistoryTab />}
            {tab === 'Charges' && <ChargesPanel />}
          </>
        )}
      </div>
    </main>
  );
}

function StatusCard({ label, dotClass, value, detail }: { label: string; dotClass: string; value: string; detail: string }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="label">{label}</div>
      <div className="mt-2 flex items-center gap-2 text-sm font-semibold text-gray-100">
        <span className={`h-2 w-2 rounded-full ${dotClass}`} />
        {value}
      </div>
      <p className="mt-2 text-xs text-gray-500">{detail}</p>
    </div>
  );
}

export default function Dashboard() {
  return (
    <Suspense fallback={null}>
      <DashboardContent />
    </Suspense>
  );
}
