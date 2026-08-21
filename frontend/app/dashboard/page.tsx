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
import TradingModeToggle from '../../components/TradingModeToggle';
import { getAuthToken } from '../../lib/authToken';
import { clearPinToken } from '../../lib/pinAuth';
import { api } from '../../lib/api';
import { WebSocketState } from '../../lib/useWebSocket';

const ALL_TABS = ['Simple', 'Filter', 'Silver Micro', 'Backtest', 'Compare', 'History', 'Calendar', 'Charges'] as const;
type TabName = (typeof ALL_TABS)[number];

// Comma-separated list of tab keys to hide, read from NEXT_PUBLIC_HIDDEN_TABS.
// Keys are lowercased and space-stripped so "Silver Micro" matches "silvermicro"
// or "silver micro" or "SILVER_MICRO". Missing/empty = show every tab (safe
// default so a Vercel config typo can't blank the whole dashboard).
// Alias map: the env var accepts short forms in addition to the exact
// tab name. Keep in sync with backend's _STRATEGY_TAB_MAP in engine.py.
const TAB_ALIASES: Record<string, string> = {
  silver: 'silvermicro',
};
function normalizeTabKey(raw: string): string {
  const cleaned = raw.trim().toLowerCase().replace(/[\s_-]+/g, '');
  return TAB_ALIASES[cleaned] || cleaned;
}
const HIDDEN_TABS: Set<string> = new Set(
  (process.env.NEXT_PUBLIC_HIDDEN_TABS || '')
    .split(',')
    .map(normalizeTabKey)
    .filter(Boolean),
);
function tabKey(name: string): string {
  return normalizeTabKey(name);
}
function isTabHidden(name: string): boolean {
  return HIDDEN_TABS.has(tabKey(name));
}
const TABS = ALL_TABS.filter((t) => !isTabHidden(t)) as readonly TabName[];

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
  // Fall back to the first visible tab if "Simple" is hidden in this
  // deployment. Prevents landing on a tab that renders nothing.
  const [tab, setTab] = useState<TabName>((TABS[0] as TabName | undefined) || 'Simple');
  const [ready, setReady] = useState(false);
  const [showFyersBanner, setShowFyersBanner] = useState(true);
  const [fyersLoginResult, setFyersLoginResult] = useState<'success' | 'failed' | null>(null);
  const [fyersLoginReason, setFyersLoginReason] = useState<string | null>(null);
  const [istTime, setIstTime] = useState(formatIstTime());
  const [fyersStatus, setFyersStatus] = useState<{
    connected: boolean;
    verified?: boolean;
    status: string;
    session_state?: string;
    message: string;
    trading_mode?: 'paper' | 'live';
  } | null>(null);
  const [statusReloadNonce, setStatusReloadNonce] = useState(0);
  const [engineStatus, setEngineStatus] = useState<{
    state: string;
    trading_mode?: string;
    error?: string | null;
    fyers_session_state?: string;
    fyers_recovery_id?: string | null;
    fyers_recovery_owner?: string | null;
    fyers_recovery_reason?: string | null;
    fyers_recovery_started_at?: string | null;
    fyers_recovery_settling_until?: string | null;
    fyers_recovery_last_event?: string | null;
    watchlist_count: number;
    live_feed_symbol_count?: number;
    strategies_running: string[];
    live_feed_started?: boolean;
    fyers_ws_connected?: boolean;
    fyers_ws_error?: string | null;
    fyers_ws_last_event_at?: string | null;
    fyers_ws_subscribed_symbols?: number;
    fyers_ws_first_tick_at?: string | null;
    ws_reconnect_failure_count?: number;
    ws_circuit_open_seconds_remaining?: number;
    ws_next_backoff_seconds?: number;
    auto_recovering?: boolean;
    disconnected_since_seconds?: number | null;
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
  const tradingMode = (engineStatus?.trading_mode as 'paper' | 'live' | undefined) || 'paper';
  const sessionState = fyersStatus?.session_state || engineStatus?.fyers_session_state || 'token_missing';
  const fyersConnectedForMode = Boolean(
    fyersStatus?.verified
    && (!fyersStatus.trading_mode || fyersStatus.trading_mode === tradingMode)
  );
  const sessionRecovering = sessionState === 'token_present_settling' || sessionState === 'token_present_ws_recovering';
  const hasUsableFyersSession = sessionState !== 'token_missing';
  const tradingReady = Boolean(hasUsableFyersSession && engineStatus?.state === 'running');
  const statusText = fyersConnectedForMode
    ? 'LIVE'
    : sessionState === 'token_missing'
      ? 'TOKEN MISSING'
      : sessionState === 'token_present_settling'
        ? 'VERIFYING'
        : sessionState === 'token_present_ws_recovering'
          ? 'RECOVERING'
          : fyersStatus?.status === 'expired'
            ? 'EXPIRED'
            : 'DEGRADED';
  const wsText = wsStatus === 'connected' ? 'Live' : wsStatus === 'reconnecting' ? 'Reconnecting' : 'Offline';
  const statusIconTone = fyersConnectedForMode
    ? 'text-[#22c55e]'
    : sessionState === 'token_missing'
      ? 'text-[#f59e0b]'
      : sessionRecovering || fyersStatus?.status === 'checking' || fyersStatus?.status === 'rechecking' || fyersStatus?.status === 'degraded'
        ? 'text-[#f59e0b]'
        : 'text-[#ef4444]';
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
    const result = searchParams.get('fyers_login');
    if (result !== 'success' && result !== 'failed') return;

    setFyersLoginResult(result);
    setFyersLoginReason(searchParams.get('reason'));
    setShowFyersBanner(true);
    const nextParams = new URLSearchParams(searchParams.toString());
    nextParams.delete('fyers_login');
    nextParams.delete('reason');
    const query = nextParams.toString();
    router.replace(query ? `/dashboard?${query}` : '/dashboard', { scroll: false });
  }, [router, searchParams]);

  useEffect(() => {
    if (!ready) return;

    let cancelled = false;
    async function loadFyersStatus() {
      try {
        const status = await api.fyersStatus();
        if (!cancelled) setFyersStatus(status);
      } catch (error) {
        if (!cancelled) {
          setFyersStatus((current) => current ? {
            ...current,
            session_state: current.session_state || 'token_present_degraded',
            status: current.verified ? 'degraded' : current.status,
            message: current.verified
              ? `Connection check temporarily unavailable; keeping the last confirmed session. ${
                  error instanceof Error ? error.message : ''
                }`.trim()
              : (error instanceof Error ? error.message : 'Unable to check Fyers status'),
          } : {
            connected: false,
            verified: false,
            status: 'checking',
            session_state: 'token_present_degraded',
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
  }, [ready, fyersLoginResult, statusReloadNonce]);

  if (!ready) return null;

  return (
    <main className="min-h-screen overflow-x-hidden bg-[#0a0e14]" data-ai-active-tab={tab}>
      <div className="mx-auto max-w-[1400px] px-3 py-3 sm:px-6 sm:py-4">
        {fyersLoginResult && showFyersBanner && (
          <div
            className={`mb-3 flex items-start justify-between gap-3 rounded border px-3 py-2 ${
              fyersLoginResult === 'success'
                ? 'border-[#22c55e]/40 bg-[#22c55e]/10'
                : 'border-[#ef4444]/40 bg-[#ef4444]/10'
            }`}
          >
            <div className="flex-1">
              <div className="text-sm text-gray-100">
                {fyersLoginResult === 'success'
                  ? 'Fyers login successful'
                  : 'Fyers login failed'}
              </div>
              {fyersLoginResult === 'failed' && fyersLoginReason && (
                <div className="mt-1 text-xs text-gray-300 break-words">
                  {fyersLoginReason}
                  {(fyersLoginReason.includes('429')
                    || fyersLoginReason.toLowerCase().includes('cloudflare')
                    || fyersLoginReason.toLowerCase().includes('too many')) && (
                    <span className="ml-2 rounded bg-[#f59e0b]/20 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[#fbbf24]">
                      Wait 5–10 min and retry
                    </span>
                  )}
                </div>
              )}
              {fyersLoginResult === 'failed' && !fyersLoginReason && (
                <div className="mt-1 text-xs text-gray-400">
                  Try again in 60 seconds. If it keeps failing, wait 5–10 minutes for Cloudflare rate limit to clear.
                </div>
              )}
            </div>
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
            <TradingModeToggle
              mode={engineStatus?.trading_mode}
              onModeChanged={(mode) => {
                setFyersStatus({
                  connected: false,
                  verified: false,
                  status: 'checking',
                  message: `Switching to FYERS ${mode.toUpperCase()} and verifying session...`,
                  trading_mode: mode,
                });
                setEngineStatus((current) => (
                  current ? { ...current, trading_mode: mode } : current
                ));
                setWsStatus('reconnecting');
                setStatusReloadNonce((value) => value + 1);
              }}
            />
            <FyersLoginButton
              connected={fyersConnectedForMode}
              mode={tradingMode}
              autoRecovering={Boolean(engineStatus?.auto_recovering)}
              sessionState={sessionState}
            />
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
                value={fyersStatus ? (fyersConnectedForMode ? 'Connected' : sessionState.replaceAll('_', ' ')) : 'Checking'}
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
            {!isTabHidden('Simple') && (
              <div className={tab === 'Simple' ? '' : 'hidden'}>
                <AlgoTab
                  key={`algo1-${tradingMode}`}
                  algoId="algo1"
                  displayName="UN1 9:15 v15 - Simple"
                  description="Uses the 9:15 signal candle only. Open=low gives BUY, open=high gives SELL, max 2% opening gap, 9:16 entry, 2% target, 1% stop loss."
                  tradingMode={tradingMode}
                  fyersConnected={fyersConnectedForMode}
                  onWebSocketStatus={setWsStatus}
                />
              </div>
            )}
            {!isTabHidden('Filter') && (
              <div className={tab === 'Filter' ? '' : 'hidden'}>
                <AlgoTab
                  key={`algo2-${tradingMode}`}
                  algoId="algo2"
                  displayName="UN1 9:15 v14 - Filter"
                  description="Uses the 9:15 signal candle only, then applies the UN1 v14 liquidity, volume, and price-range checks before the 9:16 entry. Advanced indicator filters remain optional in Settings."
                  tradingMode={tradingMode}
                  fyersConnected={fyersConnectedForMode}
                  onWebSocketStatus={setWsStatus}
                />
              </div>
            )}
            {!isTabHidden('Silver Micro') && (
              <div className={tab === 'Silver Micro' ? '' : 'hidden'}>
                <AlgoTab
                  key={`algo3-${tradingMode}`}
                  algoId="algo3"
                  displayName="Silver Micro - MCX:SILVERMIC26AUGFUT - 15m EMA breakout"
                  description="Tracks MCX:SILVERMIC26AUGFUT on 15-minute candles. BUY setup: a green candle closes above EMA20 — its close is stored as the BUY level (overwrites on each new qualifier). BUY trigger: live LTP crosses (setup close + n) in the upward direction; enter at LTP. SELL mirrors the same logic on red candles below EMA20 with (setup close - n). Reversal on opposite trigger. n is configurable (default 150 points)."
                  tradingMode={tradingMode}
                  fyersConnected={fyersConnectedForMode}
                  onWebSocketStatus={setWsStatus}
                />
              </div>
            )}
            {tab === 'Backtest' && !isTabHidden('Backtest') && <BacktestTab />}
            {tab === 'Compare' && !isTabHidden('Compare') && <CompareTab />}
            {tab === 'History' && !isTabHidden('History') && (
              <HistoryTab
                tradingMode={tradingMode}
                fyersConnected={fyersConnectedForMode}
                onFyersDisconnected={() => {
                  setFyersStatus({
                    connected: false,
                    verified: false,
                    status: 'disconnected',
                    message: `FYERS ${tradingMode} token was disconnected.`,
                    trading_mode: tradingMode,
                  });
                  setFyersLoginResult(null);
                  setShowFyersBanner(false);
                }}
              />
            )}
            {tab === 'Calendar' && !isTabHidden('Calendar') && <CalendarTab />}
            {tab === 'Charges' && !isTabHidden('Charges') && <ChargesPanel />}
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
        value={(() => {
          if (engineStatus?.fyers_ws_connected) return 'Connected';
          const circuit = engineStatus?.ws_circuit_open_seconds_remaining ?? 0;
          if (circuit > 0) {
            const m = Math.floor(circuit / 60);
            const s = circuit % 60;
            return m > 0 ? `Paused ${m}m ${s}s` : `Paused ${s}s`;
          }
          // F14: don't show "Disconnected" for transient hiccups (<60s).
          // Fyers server-closes idle WS every ~30s, so brief drops are
          // normal and shouldn't scare the user into clicking logout.
          const downFor = engineStatus?.disconnected_since_seconds ?? 0;
          if (engineStatus?.auto_recovering || downFor < 60) return 'Reconnecting…';
          return 'Disconnected';
        })()}
        tone={(() => {
          if (engineStatus?.fyers_ws_connected) return 'text-[#22c55e]';
          const circuit = engineStatus?.ws_circuit_open_seconds_remaining ?? 0;
          if (circuit > 0) return 'text-[#f59e0b]';
          const downFor = engineStatus?.disconnected_since_seconds ?? 0;
          if (engineStatus?.auto_recovering || downFor < 60) return 'text-[#f59e0b]';
          return 'text-[#ef4444]';
        })()}
        detail={(() => {
          const circuit = engineStatus?.ws_circuit_open_seconds_remaining ?? 0;
          const failures = engineStatus?.ws_reconnect_failure_count ?? 0;
          const nextBackoff = engineStatus?.ws_next_backoff_seconds ?? 0;
          const err = engineStatus?.fyers_ws_error;
          const downFor = engineStatus?.disconnected_since_seconds ?? 0;
          if (circuit > 0) {
            return `Circuit breaker open after ${failures} failures. Auto-retry in ${Math.floor(circuit / 60)}m ${circuit % 60}s. Positions monitored via REST poll (10s).`;
          }
          if (!engineStatus?.fyers_ws_connected && (engineStatus?.auto_recovering || downFor < 60)) {
            return `Brief Fyers disconnects are normal (Fyers closes idle sockets every ~30s). Auto-reconnecting${downFor > 0 ? ` (${downFor}s)` : ''}.`;
          }
          if (err && !engineStatus?.fyers_ws_connected) {
            return `${String(err).slice(0, 120)}${failures > 0 ? ` (attempt ${failures + 1}, next backoff ${nextBackoff}s)` : ''}`;
          }
          if (engineStatus?.fyers_ws_first_tick_at) return `First tick ${formatRelativeTime(engineStatus.fyers_ws_first_tick_at)}`;
          return 'Socket open; no market tick yet';
        })()}
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
        value={`${engineStatus?.symbols_with_ticks || 0} / ${engineStatus?.live_feed_symbol_count || engineStatus?.watchlist_count || 0} symbols`}
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
