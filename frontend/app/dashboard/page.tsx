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

const TABS = ['Algo 1', 'Algo 2', 'Compare', 'History', 'Charges'] as const;

function DashboardContent() {
  const [tab, setTab] = useState<(typeof TABS)[number]>('Algo 1');
  const [ready, setReady] = useState(false);
  const [showFyersBanner, setShowFyersBanner] = useState(true);
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

  useEffect(() => {
    getAuthToken().then((token) => {
      if (!token) router.replace('/login');
      else setReady(true);
    });
  }, [router]);

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
    <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      {fyersLogin && showFyersBanner && (
        <div
          className={`mb-4 flex items-center justify-between gap-3 rounded-lg border px-3 py-2 ${
            fyersLogin === 'success' ? 'border-success bg-success/15' : 'border-danger bg-danger/15'
          }`}
        >
          <span className="text-sm text-white">
            {fyersLogin === 'success' ? 'Fyers login successful' : 'Fyers login failed, try again'}
          </span>
          <button
            onClick={() => setShowFyersBanner(false)}
            className="text-sm text-textSoft transition hover:text-white"
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="mb-6 flex flex-col gap-4 border-b border-line pb-5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-textSoft">Paper trading console</p>
          <h1 className="mt-1 text-3xl font-semibold text-white">Algo Paper Trading</h1>
        </div>
        <div className="flex flex-wrap items-start justify-end gap-2">
          <div
            title={engineStatus?.error || `${engineStatus?.watchlist_count || 0} symbols loaded`}
            className={`rounded-md border px-3 py-2 text-sm font-semibold ${
              engineStatus?.state === 'running'
                ? 'border-success/70 bg-success/15 text-success'
                : 'border-danger/70 bg-danger/15 text-danger'
            }`}
          >
            Engine: {engineStatus?.state === 'running' ? 'Running' : engineStatus?.state || 'Checking...'}
          </div>
          <div
            title={fyersStatus?.message || 'Checking Fyers connection'}
            className={`rounded-md border px-3 py-2 text-sm font-semibold ${
              fyersStatus?.connected
                ? 'border-success/70 bg-success/15 text-success'
                : 'border-danger/70 bg-danger/15 text-danger'
            }`}
          >
            Fyers: {fyersStatus ? (fyersStatus.connected ? 'Connected' : 'Disconnected') : 'Checking...'}
          </div>
          <FyersLoginButton />
          <button
            onClick={async () => { clearPinToken(); await supabase.auth.signOut(); router.replace('/login'); }}
            className="rounded-md border border-line px-3 py-2 text-sm text-textSoft transition hover:border-white/30 hover:text-white"
          >
            Log out
          </button>
        </div>
      </div>

      {!tradingReady ? (
        <section className="panel p-5">
          <h2 className="text-xl font-semibold text-white">Connect Fyers to start polling</h2>
          <p className="mt-3 text-sm text-textSoft">
            The dashboard will not call algo summary, positions, trades, watchlist, or market-history endpoints until
            Fyers is connected and the trading engine is running.
          </p>
          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg border border-line bg-panelSoft p-4">
              <div className="text-xs uppercase tracking-[0.14em] text-textSoft">Fyers</div>
              <div className={`mt-1 font-semibold ${fyersStatus?.connected ? 'text-success' : 'text-danger'}`}>
                {fyersStatus ? (fyersStatus.connected ? 'Connected' : 'Disconnected') : 'Checking...'}
              </div>
              <p className="mt-2 text-sm text-textSoft">
                {fyersStatus?.message || 'Waiting for broker status check.'}
              </p>
            </div>
            <div className="rounded-lg border border-line bg-panelSoft p-4">
              <div className="text-xs uppercase tracking-[0.14em] text-textSoft">Engine</div>
              <div className={`mt-1 font-semibold ${engineStatus?.state === 'running' ? 'text-success' : 'text-danger'}`}>
                {engineStatus?.state || 'Checking...'}
              </div>
              <p className="mt-2 text-sm text-textSoft">
                {engineStatus?.error || `${engineStatus?.watchlist_count || 0} symbols loaded.`}
              </p>
            </div>
          </div>
        </section>
      ) : (
        <>
          <div className="mb-6 flex flex-wrap gap-2">
            {TABS.map((t) => (
              <button
                key={t} onClick={() => setTab(t)}
                className={`rounded-md px-4 py-2 text-sm font-medium transition ${
                  tab === t ? 'bg-success text-ink' : 'bg-panelSoft text-textSoft hover:text-white'
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          {tab === 'Algo 1' && <AlgoTab algoId="algo1" displayName="Algo 1 - Opening Range Gap" />}
          {tab === 'Algo 2' && <AlgoTab algoId="algo2" displayName="Algo 2 - VWAP/EMA/Volume Momentum" />}
          {tab === 'Compare' && <CompareTab />}
          {tab === 'History' && <HistoryTab />}
          {tab === 'Charges' && <ChargesPanel />}
        </>
      )}
    </main>
  );
}

export default function Dashboard() {
  return (
    <Suspense fallback={null}>
      <DashboardContent />
    </Suspense>
  );
}
