'use client';
import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { supabase } from '../../lib/supabaseClient';
import AlgoTab from '../../components/AlgoTab';
import CompareTab from '../../components/CompareTab';
import CalendarTab from '../../components/CalendarTab';
import ChargesPanel from '../../components/ChargesPanel';
import HistoryTab from '../../components/HistoryTab';
import BacktestTab from '../../components/BacktestTab';
import FyersLoginButton from '../../components/FyersLoginButton';
import { getAuthToken } from '../../lib/authToken';
import { clearPinToken } from '../../lib/pinAuth';
import { api } from '../../lib/api';
import { WebSocketState } from '../../lib/useWebSocket';

const TABS = ['Simple', 'Filter', 'Test Algo', 'Backtest', 'Compare', 'History', 'Calendar', 'Charges'] as const;

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
  const [tab, setTab] = useState<(typeof TABS)[number]>('Simple');
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
    live_feed_started?: boolean;
    fyers_ws_connected?: boolean;
    fyers_ws_error?: string | null;
    fyers_ws_last_event_at?: string | null;
    fyers_ws_subscribed_symbols?: number;
    fyers_ws_first_tick_at?: string | null;
    last_tick_at?: string | null;
    last_tick_symbol?: string | null;
    last_tick_ltp?: number | null;
    tick_count?: number;
    symbols_with_ticks?: number;
    last_candle_close_at?: string | null;
    closed_candle_count?: number;
  } | null>(null);
  const [wsStatus, setWsStatus] = useState<WebSocketState>('reconnecting');
  const router = useRouter();
  const searchParams = useSearchParams();
  const fyersLogin = searchParams.get('fyers_login');
  const tradingReady = Boolean(fyersStatus?.connected && engineStatus?.state === 'running');
  const statusText = fyersStatus?.connected ? 'LIVE' : fyersStatus?.status === 'disconnected' ? 'TOKEN MISSING' : 'STOPPED';
  const wsText = wsStatus === 'connected' ? 'Live' : wsStatus === 'reconnecting' ? 'Reconnecting' : 'Offline';
  const statusIconTone = fyersStatus?.connected ? 'text-[#22c55e]' : fyersStatus?.status === 'disconnected' ? 'text-[#f59e0b]' : 'text-[#ef4444]';
  const wsIconTone = wsStatus === 'connected' ? 'text-[#22c55e]' : wsStatus === 'reconnecting' ? 'text-[#f59e0b]' : 'text-[#ef4444]';

  useEffect(() => {
    getAuthToken().then((token) => {
      if (!token) router.replace('/login');
      else setReady(true);
    });
  }, [router]);

  useEffect(() => {
    const handleExpiredAuth = () => router.replace('/login');
    window.addEventListener('algo-auth-expired', handleExpiredAuth);
    return () => window.removeEventListener('algo-auth-expired', handleExpiredAuth);
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
    }, 10_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [ready, fyersLogin]);

  if (!ready) return null;

  return (
    <main className="min-h-screen overflow-x-hidden bg-[#0a0e14]" data-ai-active-tab={tab}>
      <div className="mx-auto max-w-[1400px] px-3 py-3 sm:px-6 sm:py-4">
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
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 sm:gap-x-5">
            <div className="font-mono text-sm font-semibold tracking-[0.18em] text-gray-100 sm:text-base">ALGO TRADING</div>
            <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-gray-400">
              <i className={`ri-checkbox-blank-circle-fill text-[8px] ${statusIconTone}`} />
              <span>{statusText}</span>
            </div>
            <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-gray-400">
              <i className={`ri-checkbox-blank-circle-fill text-[8px] ${wsIconTone}`} />
              <span>WS {wsText}</span>
            </div>
            <div className="font-mono text-xs tabular-nums text-gray-300 sm:text-sm">{istTime} IST</div>
            <div
              title={engineStatus?.error || `${engineStatus?.watchlist_count || 0} symbols loaded`}
              className="flex items-center gap-2 text-xs uppercase tracking-wider text-gray-400"
            >
              <i className={`ri-checkbox-blank-circle-fill text-[8px] ${engineStatus?.state === 'running' ? 'text-[#22c55e]' : 'text-[#f59e0b]'}`} />
              <span>Engine {engineStatus?.state || 'checking'}</span>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 sm:gap-3">
            <FyersLoginButton connected={Boolean(fyersStatus?.connected)} />
            <button
              onClick={async () => { clearPinToken(); await supabase.auth.signOut(); router.replace('/login'); }}
              className="inline-flex min-h-10 items-center gap-1 text-sm text-gray-500 hover:text-gray-100"
            >
              <i className="ri-logout-box-fill text-sm" />
              Logout
            </button>
          </div>
        </header>

        <nav className="mb-4 flex gap-6 overflow-x-auto whitespace-nowrap border-b border-[#1f2937] [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`min-h-10 whitespace-nowrap border-b-2 px-0 py-3 text-sm font-medium ${
                tab === t
                  ? 'border-[#3b82f6] text-gray-100'
                  : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              {t}
            </button>
          ))}
        </nav>

        {tradingReady && <LiveDiagnostics engineStatus={engineStatus} />}

        {!tradingReady && !['Backtest', 'Compare', 'History', 'Calendar', 'Charges'].includes(tab) ? (
          <section className="panel p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-gray-100">
              <i className={`ri-checkbox-blank-circle-fill text-[8px] ${statusIconTone}`} />
              Connect Fyers to start polling
            </div>
            <p className="mt-2 text-sm text-gray-500">
              The dashboard will not call algo summary, positions, trades, watchlist, or market-history endpoints until
              Fyers is connected and the trading engine is running.
            </p>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <StatusCard
                label="Fyers"
                dotClass={statusIconTone}
                value={fyersStatus ? (fyersStatus.connected ? 'Connected' : fyersStatus.status) : 'Checking'}
                detail={fyersStatus?.message || 'Waiting for broker status check.'}
              />
              <StatusCard
                label="Engine"
                dotClass={engineStatus?.state === 'running' ? 'text-[#22c55e]' : 'text-[#f59e0b]'}
                value={engineStatus?.state || 'Checking'}
                detail={engineStatus?.error || `${engineStatus?.watchlist_count || 0} symbols loaded.`}
              />
            </div>
          </section>
        ) : (
          <>
            {tab === 'Simple' && (
              <AlgoTab
                algoId="algo1"
                displayName="UN1 9:15 v15 - Simple"
                description="Ranks the combined 9:15-9:17 opening window. Open=low gives BUY, open=high gives SELL, max 2% opening gap, 9:18 entry, 2% target, 1% stop loss."
                onWebSocketStatus={setWsStatus}
              />
            )}
            {tab === 'Filter' && (
              <AlgoTab
                algoId="algo2"
                displayName="UN1 9:15 v14 - Filter"
                description="Ranks the combined 9:15-9:17 opening window, then applies the UN1 v14 liquidity, volume, and price-range checks before the 9:18 entry. Advanced indicator filters remain optional in Settings."
                onWebSocketStatus={setWsStatus}
              />
            )}
            {tab === 'Test Algo' && (
              <AlgoTab
                algoId="test_algo"
                displayName="Test Algo - Live Feature Check"
                description="Testing-only paper strategy. After 9:20 AM, a closed 1-minute candle above +0.03% becomes BUY and below -0.03% becomes SELL, with small target/SL to verify scan, positions, trades, WebSocket, and history."
                onWebSocketStatus={setWsStatus}
              />
            )}
            {tab === 'Backtest' && <BacktestTab />}
            {tab === 'Compare' && <CompareTab />}
            {tab === 'History' && <HistoryTab />}
            {tab === 'Calendar' && <CalendarTab />}
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
        <i className={`ri-checkbox-blank-circle-fill text-[8px] ${dotClass}`} />
        {value}
      </div>
      <p className="mt-2 text-xs text-gray-500">{detail}</p>
    </div>
  );
}

function LiveDiagnostics({ engineStatus }: { engineStatus: any }) {
  const hasRecentTick = isRecent(engineStatus?.last_tick_at, 90);
  const subscribedSymbols = Number(engineStatus?.fyers_ws_subscribed_symbols || 0);
  return (
    <section className="mb-4 grid gap-2 rounded border border-[#1f2937] bg-[#111827] p-3 text-xs sm:grid-cols-2 lg:grid-cols-6">
      <DiagnosticItem
        label="Fyers Feed"
        value={hasRecentTick ? 'Receiving ticks' : subscribedSymbols ? 'Subscribed, waiting for tick' : engineStatus?.live_feed_started ? 'Start requested' : 'Not started'}
        tone={hasRecentTick ? 'text-[#22c55e]' : engineStatus?.live_feed_started ? 'text-[#f59e0b]' : 'text-[#ef4444]'}
        detail={subscribedSymbols ? `${subscribedSymbols} symbols subscribed` : undefined}
      />
      <DiagnosticItem
        label="Fyers WS"
        value={engineStatus?.fyers_ws_connected ? 'Connected' : 'Disconnected'}
        tone={engineStatus?.fyers_ws_connected ? 'text-[#22c55e]' : 'text-[#ef4444]'}
        detail={engineStatus?.fyers_ws_error ? String(engineStatus.fyers_ws_error).slice(0, 80) : engineStatus?.fyers_ws_first_tick_at ? `First tick ${formatRelativeTime(engineStatus.fyers_ws_first_tick_at)}` : 'Socket open; no market tick yet'}
      />
      <DiagnosticItem
        label="Last Tick"
        value={formatRelativeTime(engineStatus?.last_tick_at)}
        tone={hasRecentTick ? 'text-[#22c55e]' : 'text-[#f59e0b]'}
      />
      <DiagnosticItem
        label="Last Symbol"
        value={engineStatus?.last_tick_symbol ? `${engineStatus.last_tick_symbol} @ ${formatNumber(engineStatus.last_tick_ltp)}` : '--'}
      />
      <DiagnosticItem
        label="Tick Coverage"
        value={`${engineStatus?.symbols_with_ticks || 0} / ${engineStatus?.watchlist_count || 0} symbols`}
      />
      <DiagnosticItem
        label="Closed Candles"
        value={`${engineStatus?.closed_candle_count || 0} total`}
        detail={engineStatus?.last_candle_close_at ? `Last ${formatRelativeTime(engineStatus.last_candle_close_at)}` : 'Waiting'}
      />
    </section>
  );
}

function DiagnosticItem({ label, value, tone = 'text-gray-100', detail }: { label: string; value: string; tone?: string; detail?: string }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#0d1117] p-2">
      <div className="label text-[10px]">{label}</div>
      <div className={`num mt-1 font-semibold ${tone}`}>{value}</div>
      {detail && <div className="mt-1 text-[11px] text-gray-500">{detail}</div>}
    </div>
  );
}

function formatNumber(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return number.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function formatRelativeTime(value?: string | null, emptyLabel = 'No ticks yet') {
  if (!value) return emptyLabel;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '--';
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}

function isRecent(value: string | null | undefined, maxAgeSeconds: number) {
  if (!value) return false;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return false;
  return Date.now() - date.getTime() <= maxAgeSeconds * 1000;
}

export default function Dashboard() {
  return (
    <Suspense fallback={null}>
      <DashboardContent />
    </Suspense>
  );
}
