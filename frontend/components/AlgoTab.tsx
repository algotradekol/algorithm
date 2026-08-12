'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import StrategySettingsPanel from './StrategySettingsPanel';
import ScanResultsPanel from './ScanResultsPanel';
import { useWebSocket, WebSocketState } from '../lib/useWebSocket';
import { PAGE_SIZE, PaginationControls } from './PaginationControls';

const FALLBACK_POLL_MS = 5_000;

// ─── Debug logger (always on — remove later if too noisy) ──────────────────
const _t = () => new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' });
const log = (...args: any[]) => console.log(`[AlgoTab ${_t()}]`, ...args);
const logWarn = (...args: any[]) => console.warn(`[AlgoTab ${_t()}]`, ...args);
const logErr = (...args: any[]) => console.error(`[AlgoTab ${_t()}]`, ...args);

export default function AlgoTab({
  algoId,
  displayName,
  description,
  tradingMode,
  fyersConnected,
  onWebSocketStatus,
}: {
  algoId: string;
  displayName: string;
  description?: string;
  tradingMode?: 'paper' | 'live';
  fyersConnected?: boolean;
  onWebSocketStatus?: (status: WebSocketState) => void;
}) {
  const [summary, setSummary] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<any[]>([]);
  const [scanResults, setScanResults] = useState<any>(null);
  const [feedStatus, setFeedStatus] = useState<any>(null);
  const [walletStatus, setWalletStatus] = useState<any>(null);
  const [walletStatusError, setWalletStatusError] = useState('');
  const [brokerPositions, setBrokerPositions] = useState<any[]>([]);
  const [brokerPositionsError, setBrokerPositionsError] = useState('');
  const [brokerOrders, setBrokerOrders] = useState<any[]>([]);
  const [brokerOrdersError, setBrokerOrdersError] = useState('');
  const [error, setError] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [exitingPositionId, setExitingPositionId] = useState<string | null>(null);
  const walletRequestId = useRef(0);
  const brokerPositionsRequestId = useRef(0);
  const brokerOrdersRequestId = useRef(0);
  const dataRequestId = useRef(0);

  const refreshSummary = useCallback(async () => {
    try {
      const nextSummary = await api.summary(algoId);
      setSummary(nextSummary);
    } catch {
      // Keep the current summary if the lightweight refresh fails.
    }
  }, [algoId]);

  const refreshPositions = useCallback(async () => {
    try {
      const nextPositions = await api.positions(algoId);
      setPositions(nextPositions.map((position: any) => ({
        ...position,
        ltp: position.ltp ?? position.last_ltp ?? position._last_ltp ?? position.entry_price,
        unrealized_pnl: position.unrealized_pnl ?? 0,
      })));
    } catch {
      // Keep the current open-position list if the lightweight refresh fails.
    }
  }, [algoId]);

  const refreshTrades = useCallback(async () => {
    try {
      const nextTrades = await api.trades(algoId);
      setTrades(nextTrades);
    } catch {
      // Keep the current trade list if the lightweight refresh fails.
    }
  }, [algoId]);

  const loadData = useCallback(async () => {
    const requestId = ++dataRequestId.current;
    // Polling logs removed - see backend debug report instead
    const [summaryResult, positionsResult, tradesResult, scanResult, feedResult] = await Promise.allSettled([
      api.summary(algoId), api.positions(algoId), api.trades(algoId), api.scanResults(algoId),
      algoId === 'algo3' ? api.feedStatus(algoId) : Promise.resolve(null),
    ]);
    if (requestId !== dataRequestId.current) {
      log(`⚡ stale req#${requestId} discarded`);
      return;
    }

    if (summaryResult.status === 'fulfilled') {
      setSummary(summaryResult.value);
    } else {
      logErr(`summary failed:`, (summaryResult as PromiseRejectedResult).reason);
    }

    if (positionsResult.status === 'fulfilled') {
      const pos = positionsResult.value;
      log(`📊 positions: ${pos.length} open`);
      setPositions(pos.map((position: any) => ({
        ...position,
        ltp: position.ltp ?? position.last_ltp ?? position._last_ltp ?? position.entry_price,
        unrealized_pnl: position.unrealized_pnl ?? 0,
      })));
    } else {
      logErr(`positions failed:`, (positionsResult as PromiseRejectedResult).reason);
    }

    if (tradesResult.status === 'fulfilled') {
      log(`📋 trades: ${tradesResult.value.length} closed`);
      setTrades(tradesResult.value);
    } else {
      logErr(`trades failed:`, (tradesResult as PromiseRejectedResult).reason);
    }

    if (scanResult.status === 'fulfilled') {
      const scan = scanResult.value;
      const status = scan?.scan_status ?? scan?.status ?? 'unknown';
      const count = Array.isArray(scan?.rows) ? scan.rows.length : (scan?.total_symbols ?? '?');
      const phase = scan?.phase ?? '';
      // Scan logs removed - see backend debug report instead
      setScanResults(scan);
    } else {
      logErr(`scanResults failed:`, (scanResult as PromiseRejectedResult).reason);
    }

    if (feedResult.status === 'fulfilled') setFeedStatus(feedResult.value);

    const failures = [summaryResult, positionsResult, tradesResult]
      .filter((result) => result.status === 'rejected')
      .map((result) => (result as PromiseRejectedResult).reason?.message || 'Request failed');
    if (failures.length) logErr(`⚠️ ${failures.length} request(s) failed:`, failures);
    setError(failures[0] || '');
  }, [algoId, tradingMode]);

  const loadWalletStatus = useCallback(async () => {
    const requestId = ++walletRequestId.current;
    if (tradingMode !== 'live' || !fyersConnected) {
      setWalletStatus(null);
      setWalletStatusError('');
      return;
    }
    try {
      const result = await api.fyersFunds(tradingMode, true);
      if (requestId !== walletRequestId.current) return;
      if (result?.available !== false) {
        setWalletStatus(result);
      }
      setWalletStatusError(result?.warning || '');
    } catch (e: any) {
      if (requestId !== walletRequestId.current) return;
      // Keep the last confirmed balance visible while FYERS is unavailable.
      setWalletStatusError(e?.message || 'Failed to load FYERS wallet balance');
    }
  }, [fyersConnected, tradingMode]);

  const loadBrokerPositions = useCallback(async () => {
    const requestId = ++brokerPositionsRequestId.current;
    if (tradingMode !== 'live' || !fyersConnected) {
      setBrokerPositions([]);
      setBrokerPositionsError('');
      return;
    }
    try {
      const result = await api.fyersPositions(tradingMode);
      if (requestId !== brokerPositionsRequestId.current) return;
      if (result?.available !== false) {
        setBrokerPositions(Array.isArray(result?.positions) ? result.positions : []);
      }
      setBrokerPositionsError(result?.warning || '');
    } catch (e: any) {
      if (requestId !== brokerPositionsRequestId.current) return;
      // Preserve the last known broker snapshot during a transient FYERS or
      // Railway outage. Clearing it makes an open live trade disappear.
      setBrokerPositionsError(e?.message || 'Failed to load FYERS broker positions');
    }
  }, [fyersConnected, tradingMode]);

  const loadBrokerOrders = useCallback(async () => {
    const requestId = ++brokerOrdersRequestId.current;
    if (tradingMode !== 'live' || !fyersConnected) {
      setBrokerOrders([]);
      setBrokerOrdersError('');
      return;
    }
    try {
      const result = await api.fyersOrders(tradingMode, true);
      if (requestId !== brokerOrdersRequestId.current) return;
      if (result?.available !== false) {
        setBrokerOrders(Array.isArray(result?.orders) ? result.orders : []);
      }
      setBrokerOrdersError(result?.warning || '');
    } catch (e: any) {
      if (requestId !== brokerOrdersRequestId.current) return;
      setBrokerOrdersError(e?.message || 'Failed to load FYERS pending orders');
    }
  }, [fyersConnected, tradingMode]);

  useEffect(() => {
    dataRequestId.current += 1;
    walletRequestId.current += 1;
    brokerPositionsRequestId.current += 1;
    brokerOrdersRequestId.current += 1;
    api.clearFyersAccountCache();
    setSummary(null);
    setPositions([]);
    setTrades([]);
    setScanResults(null);
    setFeedStatus(null);
    setWalletStatus(null);
    setWalletStatusError('');
    setBrokerPositions([]);
    setBrokerPositionsError('');
    setBrokerOrders([]);
    setBrokerOrdersError('');
    setError('');
  }, [tradingMode]);

  useEffect(() => {
    let cancelled = false;
    // Mount logs removed - see backend debug report instead
    loadData();
    const interval = setInterval(() => {
      if (!document.hidden && !cancelled) {
        loadData();
      } else if (document.hidden) {
        log(`🙈 tab hidden — skipping poll`);
      }
    }, FALLBACK_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
      log(`🛑 AlgoTab unmounted for ${algoId}`);
    };
  }, [loadData]);

  useEffect(() => {
    let cancelled = false;
    async function refreshWallet() {
      await loadWalletStatus();
      if (cancelled) return;
    }
    refreshWallet();
    const interval = setInterval(() => {
      if (!document.hidden && !cancelled) refreshWallet();
    }, 15_000);
    const refreshWhenVisible = () => {
      if (!document.hidden && !cancelled) refreshWallet();
    };
    window.addEventListener('focus', refreshWhenVisible);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      cancelled = true;
      walletRequestId.current += 1;
      clearInterval(interval);
      window.removeEventListener('focus', refreshWhenVisible);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [loadWalletStatus]);

  useEffect(() => {
    let cancelled = false;
    async function refreshBrokerPositions() {
      await loadBrokerPositions();
      if (cancelled) return;
    }
    refreshBrokerPositions();
    const interval = setInterval(() => {
      if (!document.hidden && !cancelled) refreshBrokerPositions();
    }, 15_000);
    return () => {
      cancelled = true;
      brokerPositionsRequestId.current += 1;
      clearInterval(interval);
    };
  }, [loadBrokerPositions]);

  useEffect(() => {
    let cancelled = false;
    async function refreshBrokerOrders() {
      await loadBrokerOrders();
      if (cancelled) return;
    }
    refreshBrokerOrders();
    const interval = setInterval(() => {
      if (!document.hidden && !cancelled) refreshBrokerOrders();
    }, 15_000);
    return () => {
      cancelled = true;
      brokerOrdersRequestId.current += 1;
      clearInterval(interval);
    };
  }, [loadBrokerOrders]);

  const handleWsMessage = useCallback((message: any) => {
    if (message.event === 'price_update') {
      // WS tick logs removed - too noisy
      setPositions((current) => current.map((position) => (
        position.symbol === message.symbol ? {
          ...position,
          ltp: message.ltp,
          high_price: Math.max(Number(position.high_price ?? position.highest_price ?? position.entry_price ?? message.ltp), Number(message.ltp)),
          low_price: Math.min(Number(position.low_price ?? position.lowest_price ?? position.entry_price ?? message.ltp), Number(message.ltp)),
          unrealized_pnl: calculateUnrealized(position, message.ltp),
        } : position
      )));
      setBrokerPositions((current) => current.map((position) => {
        if (position.symbol !== message.symbol) return position;
        const ltp = Number(message.ltp);
        const entry = Number(position.entry_price || 0);
        const qty = Math.abs(Number(position.net_qty ?? position.qty ?? 0));
        const unrealized = (position.side === 'SELL' ? entry - ltp : ltp - entry) * qty;
        return {
          ...position,
          ltp,
          unrealized_pnl: unrealized,
          total_pnl: Number(position.realized_pnl || 0) + unrealized,
        };
      }));
      return;
    }

    if (message.algo_id !== algoId) return;

    if (message.event === 'position_opened') {
      setPositions((current) => [{ ...message, ltp: message.ltp ?? message.entry_price, status: 'open' }, ...current]);
      refreshSummary();
    } else if (message.event === 'position_closed') {
      setPositions((current) => current.filter((position) => position.symbol !== message.symbol));
      setTrades((current) => [message, ...current]);
      refreshSummary();
    } else if (message.event === 'scan_complete') {
      setScanResults(message.results);
    }
  }, [algoId, refreshSummary]);

  useWebSocket(handleWsMessage, true, onWebSocketStatus);

  const exitPosition = useCallback(async (position: any) => {
    if (!position.id) {
      setError('This legacy position has no ID and cannot be exited from the dashboard.');
      return;
    }
    const confirmed = window.confirm(`Exit ${position.symbol} now at the latest Fyers price? This closes the ${tradingMode === 'live' ? 'LIVE' : 'paper'} position immediately.`);
    if (!confirmed) return;

    setExitingPositionId(String(position.id));
    setError('');
    try {
      await api.exitPosition(algoId, String(position.id));
      setPositions((current) => current.filter((row) => String(row.id) !== String(position.id)));
      await Promise.all([refreshSummary(), refreshTrades()]);
    } catch (exitError: any) {
      setError(exitError?.message || 'Could not exit the position.');
    } finally {
      setExitingPositionId(null);
    }
  }, [algoId, refreshSummary, refreshTrades]);

  if (!summary) {
    return (
      <section className="panel p-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-gray-100">{displayName}</h2>
            {description && <p className="mt-1 text-xs text-gray-500">{description}</p>}
          </div>
          <button
            onClick={() => setSettingsOpen((open) => !open)}
            className="min-h-10 rounded border border-[#3b82f6] px-3 py-1.5 text-xs font-semibold text-[#3b82f6]"
          >
            Settings
          </button>
        </div>
        <SettingsDrawer open={settingsOpen} algoId={algoId} tradingMode={tradingMode} onClose={() => setSettingsOpen(false)} />
        <div className="mt-4">
        <ScanResultsPanel
          algoId={algoId}
          results={scanResults}
          openPositions={positions}
          onRefresh={async () => {
            await Promise.allSettled([refreshSummary(), refreshPositions()]);
          }}
        />
        </div>
        <p className="mt-2 text-sm text-gray-500">{error || 'Loading strategy data...'}</p>
      </section>
    );
  }

  const startingCapital = Number(summary.starting_capital || 0);
  const cash = Number(summary.cash || 0);
  const netPnl = Number(summary.realized_net_pnl || 0);
  const grossPnl = Number(summary.realized_gross_pnl || 0);
  const walletSummary = walletStatus?.summary || {};
  const liveWalletBalance = optionalNumber(walletSummary.wallet_balance);
  const showLiveWallet = tradingMode === 'live';
  const openUnrealizedPnl = positions.reduce((total, position) => {
    const ltp = Number(position.ltp ?? position.last_ltp ?? position._last_ltp ?? position.entry_price);
    const entry = Number(position.entry_price || 0);
    const qty = Number(position.qty || 0);
    if (!Number.isFinite(ltp) || !Number.isFinite(entry) || !Number.isFinite(qty)) return total;
    return total + (position.side === 'SELL' ? entry - ltp : ltp - entry) * qty;
  }, 0);
  const capitalUsed = positions.reduce((total, position) => total + Number(position.entry_price || 0) * Number(position.qty || 0), 0);
  // Cash already includes closed-trade P&L. Add open mark-to-market movement
  // once so Total Capital represents the live paper-account value.
  const totalCapital = cash + openUnrealizedPnl;
  const liveNetPnl = netPnl + openUnrealizedPnl;
  const managedPositionKeys = new Set(
    positions.map((position) => `${position.symbol}|${position.side}`),
  );
  const brokerPositionKeys = new Set(
    brokerPositions.map((position) => `${position.symbol}|${position.side}`),
  );
  const openPositionRows = [
    ...positions.map((position) => ({
      ...position,
      position_source: 'algorithm',
    })),
    ...brokerPositions
      .filter((position) => !managedPositionKeys.has(`${position.symbol}|${position.side}`))
      .map((position) => ({
        ...position,
        position_source: 'fyers_app',
        is_broker_position: true,
        entry_trigger: 'Opened directly in FYERS app',
      })),
  ];

  return (
    <section className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-gray-100">{displayName}</h2>
          {description && <p className="mt-1 text-xs text-gray-500">{description}</p>}
        </div>
        <button
          onClick={() => setSettingsOpen((open) => !open)}
          className="min-h-10 rounded border border-[#3b82f6] px-3 py-1.5 text-xs font-semibold text-[#3b82f6]"
        >
          Settings
        </button>
      </div>
      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}
      {walletStatusError && tradingMode === 'live' && fyersConnected && (
        <p className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">
          {walletStatusError}
        </p>
      )}
      {brokerPositionsError && tradingMode === 'live' && fyersConnected && (
        <p className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">
          {brokerPositionsError}
        </p>
      )}
      {brokerOrdersError && tradingMode === 'live' && fyersConnected && (
        <p className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">
          {brokerOrdersError}
        </p>
      )}
      {algoId === 'algo3' && <SilverFeedPanel status={feedStatus} />}

      <div className="grid grid-cols-3 gap-1.5 sm:gap-2 lg:grid-cols-6">
        {showLiveWallet ? (
          <MetricCard
            label="Wallet Balance"
            value={liveWalletBalance === null ? '--' : formatMoney(liveWalletBalance)}
            helper={
              walletSummary.wallet_balance_source
                ? `source: ${walletSummary.wallet_balance_source}`
                : walletStatusError || 'Waiting for FYERS funds'
            }
          />
        ) : (
          <MetricCard label="Total Capital" value={formatMoney(totalCapital)} delta={formatSignedMoney(totalCapital - startingCapital)} pnl={totalCapital - startingCapital} />
        )}
        <MetricCard label="Capital Used" value={formatMoney(capitalUsed)} />
        <MetricCard label="Trades Today" value={`${summary.trade_count_today} / ${summary.max_trades_per_day || 10}`} />
        <MetricCard label="Buy / Sell" value={`${summary.buy_count_today}B ${summary.sell_count_today}S`} />
        <MetricCard label="Realized Gross P&L" value={formatMoney(grossPnl)} pnl={grossPnl} />
        <MetricCard label="Live Net P&L" value={formatMoney(liveNetPnl)} pnl={liveNetPnl} important />
      </div>

      <SettingsDrawer open={settingsOpen} algoId={algoId} tradingMode={tradingMode} onClose={() => setSettingsOpen(false)} />

      <ScanResultsPanel algoId={algoId} results={scanResults} openPositions={positions} onRefresh={loadData} />

      <div className="grid gap-4">
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">Open Positions</h3>
          <PositionsTable rows={openPositionRows} onExit={exitPosition} exitingPositionId={exitingPositionId} tradingMode={tradingMode} />
        </section>

        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">Closed Trades Today</h3>
          <TradesTable rows={trades} />
        </section>
      </div>
      {description && <div className="rounded border border-[#1f2937] bg-[#111827] px-3 py-2 text-xs text-gray-500">{description}</div>}
    </section>
  );
}

function SilverFeedPanel({ status }: { status: any }) {
  const lastTick = formatDateTime(status?.last_tick_at);
  const lastMinuteCandle = formatDateTime(status?.last_minute_candle_at);
  const lastFiveMinuteBar = formatDateTime(status?.last_five_minute_bar_at);
  const historyBadge = status?.history_loading
    ? 'loading history'
    : status?.history_ready
    ? 'history ready'
    : status?.history_error
    ? 'history error'
    : 'history pending';
  return (
    <div className="rounded border border-[#3b82f6]/30 bg-[#0b1220] p-3 text-xs text-gray-300">
      <div className="flex items-center justify-between gap-3">
        <div className="label text-[10px]">Silver feed diagnostics</div>
        <div className={`rounded px-2 py-0.5 font-semibold ${status?.last_tick_at ? 'bg-[#22c55e]/15 text-[#22c55e]' : 'bg-[#f59e0b]/15 text-[#f59e0b]'}`}>
          {status?.last_tick_at ? 'tick seen' : 'waiting for tick'}
        </div>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <FeedStat label="Symbol" value={status?.symbol || '--'} />
        <FeedStat label="State" value={historyBadge} />
        <FeedStat label="Last tick" value={lastTick || '--'} />
        <FeedStat label="Last price" value={formatNumber(status?.last_tick_ltp)} />
        <FeedStat label="Last 1m candle" value={lastMinuteCandle || '--'} />
        <FeedStat label="Last 5m bar" value={lastFiveMinuteBar || '--'} />
        <FeedStat label="5m bars" value={status?.five_minute_bars ?? 0} />
        <FeedStat label="Warmup candles" value={status?.warmup_minute_candles ?? 0} />
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-[10px] text-gray-500">
        <span>History load: {status?.history_error || status?.history_loading ? 'check logs' : 'ok'}</span>
        <span>Pending setup: {status?.pending_setup ? 'yes' : 'no'}</span>
        <span>Pending entry: {status?.pending_entry ? 'yes' : 'no'}</span>
      </div>
    </div>
  );
}

function FeedStat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#111827] p-2">
      <div className="label text-[10px]">{label}</div>
      <div className="num mt-1 truncate text-xs font-semibold text-gray-100">{String(value ?? '--')}</div>
    </div>
  );
}

function SettingsDrawer({
  open,
  algoId,
  tradingMode,
  onClose,
}: {
  open: boolean;
  algoId: string;
  tradingMode?: 'paper' | 'live';
  onClose: () => void;
}) {
  return (
    <div className={`overflow-hidden transition-opacity duration-300 ${open ? 'opacity-100' : 'max-h-0 opacity-0'}`}>
      <div className="mt-4 rounded border border-[#1f2937] bg-[#0d1117] p-3">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="label">Strategy Settings</div>
          <button onClick={onClose} className="text-sm text-gray-500 hover:text-gray-100">X</button>
        </div>
        <StrategySettingsPanel algoId={algoId} tradingMode={tradingMode} />
      </div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  delta,
  pnl,
  important,
  helper,
}: {
  label: string;
  value: string;
  delta?: string;
  pnl?: number;
  important?: boolean;
  helper?: string;
}) {
  return (
    <div className="min-w-0 rounded border border-[#1f2937] bg-[#111827] p-2 sm:p-3">
      <div className="label text-[10px] sm:text-xs">{label}</div>
      <div className={`num mt-1.5 flex min-w-0 items-center gap-1 whitespace-nowrap font-semibold sm:mt-2 ${important ? 'text-base sm:text-xl' : 'text-xs sm:text-base'} ${pnlColor(pnl)}`}>
        {label === 'Trades Today' && <i className="ri-exchange-fill text-xs text-slate-400" />}
        {pnl !== undefined && pnl > 0 && <i className="ri-arrow-up-circle-fill shrink-0 text-sm text-[#22c55e]" />}
        {pnl !== undefined && pnl < 0 && <i className="ri-arrow-down-circle-fill shrink-0 text-sm text-[#ef4444]" />}
        <span className="min-w-0 overflow-hidden text-ellipsis">{value}</span>
      </div>
      {delta && <div className={`num mt-1 truncate text-xs ${pnlColor(pnl)}`}>{delta} vs start</div>}
      {helper && <div className="mt-1 truncate text-[10px] text-gray-500">{helper}</div>}
    </div>
  );
}

function PositionsTable({
  rows,
  onExit,
  exitingPositionId,
  tradingMode,
}: {
  rows: any[];
  onExit: (row: any) => void;
  exitingPositionId: string | null;
  tradingMode?: string;
}) {
  const [page, setPage] = useState(0);
  const safePage = Math.min(page, Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1));
  const visibleRows = rows.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);
  return (
    <>
      <div className="space-y-2 sm:hidden">
        {!rows.length ? <p className="rounded border border-[#1f2937] bg-[#0d1117] p-3 text-sm text-gray-500">No open positions</p> : visibleRows.map((row, index) => {
          const ltp = Number(row.ltp ?? row.last_ltp ?? row._last_ltp);
          const entry = Number(row.entry_price || 0);
          const qty = Number(row.qty || 0);
          const unreal = Number.isFinite(Number(row.unrealized_pnl)) ? Number(row.unrealized_pnl) : Number.isFinite(ltp) ? (row.side === 'SELL' ? entry - ltp : ltp - entry) * qty : null;
          return (
            <div key={row.id || index} className={`rounded border border-[#1f2937] p-3 ${index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}`}>
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="label text-[10px]">#{safePage * PAGE_SIZE + index + 1}</div>
                  <div className="font-mono text-sm text-gray-100">{row.symbol}</div>
                </div>
                <div className={`num flex items-center gap-1 text-base font-semibold ${pnlColor(unreal)}`}>{unreal === null ? '--' : formatMoney(unreal)}</div>
              </div>
              <div className={`mt-1 inline-flex items-center gap-1 text-sm font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                <i className={`${row.side === 'SELL' ? 'ri-indeterminate-circle-fill' : 'ri-add-circle-fill'} text-sm`} />
                {row.side === 'SELL' ? 'S' : 'B'}
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-gray-500">
                <MobileField label="Source" value={<PositionSourceBadge row={row} />} wide />
                <MobileField label="Entry Time" value={formatDateTime(row.entry_time)} wide />
                <MobileField label="Qty" value={row.qty} />
                <MobileField label="Entry" value={formatNumber(row.entry_price)} />
                <MobileField label="LTP" value={Number.isFinite(ltp) ? formatNumber(ltp) : '--'} />
                <MobileField label="Position High" value={formatNumber(row.high_price ?? row.highest_price)} />
                <MobileField label="Position Low" value={formatNumber(row.low_price ?? row.lowest_price)} />
                <MobileField label="SL" value={formatNumber(row.sl_price)} />
                <MobileField label="Target" value={formatNumber(row.target_price)} />
                <MobileField label="Trailing SL" value={<TrailingBadge row={row} />} wide />
                <MobileField label="Trigger" value={formatTrigger(row.entry_trigger)} wide />
                <MobileField label="Signal Audit" value={<SignalAudit row={row} />} wide />
              </div>
              <ManualExitButton row={row} onExit={onExit} exitingPositionId={exitingPositionId} tradingMode={tradingMode} mobile />
            </div>
          );
        })}
      </div>
      <div className="hidden overflow-x-auto rounded border border-[#1f2937] sm:block">
        <table className="w-full min-w-[1180px] border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {['#', 'Symbol', 'Source', 'Side', 'Qty', 'Entry Time', 'Entry', 'LTP', 'Position High', 'Position Low', 'SL', 'Target', 'Trailing SL', 'Signal Audit', 'Trigger', 'Unreal P&L', 'Exit'].map((column) => (
              <th key={column} className="table-cell label">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={17} className="table-cell text-gray-500">No open positions</td>
            </tr>
          ) : visibleRows.map((row, index) => {
            const ltp = Number(row.ltp ?? row.last_ltp ?? row._last_ltp);
            const entry = Number(row.entry_price || 0);
            const qty = Number(row.qty || 0);
            const unreal = Number.isFinite(Number(row.unrealized_pnl))
              ? Number(row.unrealized_pnl)
              : Number.isFinite(ltp)
              ? (row.side === 'SELL' ? entry - ltp : ltp - entry) * qty
              : null;
            return (
              <tr key={row.id || index} className={index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
                <td className="table-cell num text-gray-500">{safePage * PAGE_SIZE + index + 1}</td>
                <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
                <td className="table-cell"><PositionSourceBadge row={row} /></td>
                <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                  <i className={`${row.side === 'SELL' ? 'ri-indeterminate-circle-fill' : 'ri-add-circle-fill'} mr-1 text-sm`} />
                  {row.side === 'SELL' ? 'S' : 'B'}
                </td>
                <td className="table-cell num text-gray-100">{row.qty}</td>
                <td className="table-cell num text-gray-400">{formatDateTime(row.entry_time)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.entry_price)}</td>
                <td className="table-cell num text-gray-100">{Number.isFinite(ltp) ? formatNumber(ltp) : '--'}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.high_price ?? row.highest_price)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.low_price ?? row.lowest_price)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.sl_price)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.target_price)}</td>
                <td className="table-cell min-w-[150px]"><TrailingBadge row={row} /></td>
                <td className="table-cell min-w-[190px] text-gray-400"><SignalAudit row={row} /></td>
                <td className="table-cell max-w-[300px] text-gray-400">{formatTrigger(row.entry_trigger)}</td>
                <td className={`table-cell num font-semibold ${pnlColor(unreal)}`}>{unreal === null ? '--' : formatMoney(unreal)}</td>
                <td className="table-cell"><ManualExitButton row={row} onExit={onExit} exitingPositionId={exitingPositionId} tradingMode={tradingMode} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
      <PaginationControls page={safePage} totalRows={rows.length} onPageChange={setPage} />
    </>
  );
}

function TradesTable({ rows }: { rows: any[] }) {
  const [page, setPage] = useState(0);
  const safePage = Math.min(page, Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1));
  const visibleRows = rows.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);
  return (
    <>
      <div className="space-y-2 sm:hidden">
        {!rows.length ? (
          <p className="rounded border border-[#1f2937] bg-[#0d1117] p-3 text-sm text-gray-500">No closed trades yet</p>
        ) : visibleRows.map((row, index) => (
          <div key={row.id || index} className={`rounded border border-[#1f2937] p-3 ${index % 2 === 0 ? "bg-[#111827]" : "bg-[#0d1117]"}`}>
            <div className="flex items-center justify-between gap-3">
              <div className="font-mono text-sm text-gray-100">{row.symbol}</div>
              <div className={`num flex items-center gap-1 text-base font-semibold ${pnlColor(Number(row.net_pnl || 0))}`}>
                {Number(row.net_pnl || 0) > 0 && <i className="ri-arrow-up-circle-fill text-sm text-[#22c55e]" />}
                {Number(row.net_pnl || 0) < 0 && <i className="ri-arrow-down-circle-fill text-sm text-[#ef4444]" />}
                {formatMoney(row.net_pnl)}
              </div>
            </div>
            <div className={`mt-1 inline-flex items-center gap-1 text-sm font-semibold ${row.side === "SELL" ? "text-[#ef4444]" : "text-[#22c55e]"}`}>
              <i className={`${row.side === "SELL" ? "ri-indeterminate-circle-fill" : "ri-add-circle-fill"} text-sm`} />
              {row.side === "SELL" ? "S" : "B"}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-gray-500">
              <MobileField label="Entry Time" value={formatDateTime(row.entry_time)} />
              <MobileField label="Entry" value={formatNumber(row.entry_price)} />
              <MobileField label="Exit Time" value={row.exit_time ? formatDateTime(row.exit_time) : "--"} />
              <MobileField label="Exit" value={formatNumber(row.exit_price)} />
              <MobileField label="Reason" value={formatReason(row.exit_reason)} />
              <MobileField label="Trailing SL" value={<TrailingBadge row={row} />} wide />
              <MobileField label="Gross" value={formatMoney(row.gross_pnl)} />
              <MobileField label="Charges" value={formatMoney(row.total_charges)} />
            </div>
          </div>
        ))}
      </div>
      <div className="hidden overflow-x-auto rounded border border-[#1f2937] sm:block">
        <table className="w-full min-w-[1280px] border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {["Symbol", "Side", "Entry Time", "Entry", "Exit Time", "Exit", "Reason", "Trailing SL", "Signal Audit", "Trigger", "Gross", "Charges", "Net"].map((column) => (
              <th key={column} className="table-cell label">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={13} className="table-cell text-gray-500">No closed trades yet</td>
            </tr>
          ) : visibleRows.map((row, index) => (
            <tr key={row.id || index} className={index % 2 === 0 ? "bg-[#111827]" : "bg-[#0d1117]"}>
              <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
              <td className={`table-cell font-semibold ${row.side === "SELL" ? "text-[#ef4444]" : "text-[#22c55e]"}`}>
                <i className={`${row.side === "SELL" ? "ri-indeterminate-circle-fill" : "ri-add-circle-fill"} mr-1 text-sm`} />
                {row.side === "SELL" ? "S" : "B"}
              </td>
              <td className="table-cell num text-gray-400">{formatDateTime(row.entry_time)}</td>
              <td className="table-cell num text-gray-100">{formatNumber(row.entry_price)}</td>
              <td className="table-cell num text-gray-400">{row.exit_time ? formatDateTime(row.exit_time) : "--"}</td>
              <td className="table-cell num text-gray-100">{formatNumber(row.exit_price)}</td>
              <td className={`table-cell font-semibold ${reasonColor(row.exit_reason)}`}>
                {reasonIcon(row.exit_reason)}
                {formatReason(row.exit_reason)}
              </td>
              <td className="table-cell min-w-[150px]"><TrailingBadge row={row} /></td>
              <td className="table-cell min-w-[190px] text-gray-400"><SignalAudit row={row} /></td>
              <td className="table-cell max-w-[300px] text-gray-400">{formatTrigger(row.entry_trigger)}</td>
              <td className={`table-cell num ${pnlColor(Number(row.gross_pnl || 0))}`}>{formatMoney(row.gross_pnl)}</td>
              <td className="table-cell num text-gray-100">{formatMoney(row.total_charges)}</td>
              <td className={`table-cell num font-semibold ${pnlColor(Number(row.net_pnl || 0))}`}>{formatMoney(row.net_pnl)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <PaginationControls page={safePage} totalRows={rows.length} onPageChange={setPage} />
    </>
  );
}


function ManualExitButton({
  row,
  onExit,
  exitingPositionId,
  mobile = false,
  tradingMode,
}: {
  row: any;
  onExit: (row: any) => void;
  exitingPositionId: string | null;
  mobile?: boolean;
  tradingMode?: string;
}) {
  if (row.is_broker_order || row.position_source === 'fyers_order') {
    return (
      <span
        className={`${mobile ? 'mt-3 flex w-full justify-center' : 'inline-flex'} min-h-9 items-center rounded border border-[#3b82f6]/40 px-2.5 py-1.5 text-xs font-semibold text-[#60a5fa]`}
        title="This order is still pending or scheduled in FYERS. Manage it from the FYERS app."
      >
        <i className="ri-time-fill mr-1 text-sm" />
        Pending in FYERS
      </span>
    );
  }
  if (row.is_broker_position || row.position_source === 'fyers_app') {
    return (
      <span
        className={`${mobile ? 'mt-3 flex w-full justify-center' : 'inline-flex'} min-h-9 items-center rounded border border-[#3b82f6]/40 px-2.5 py-1.5 text-xs font-semibold text-[#60a5fa]`}
        title="This position was opened outside the algorithm. Manage or exit it in the FYERS app."
      >
        <i className="ri-smartphone-fill mr-1 text-sm" />
        Manage in FYERS
      </span>
    );
  }
  const exiting = String(row.id) === exitingPositionId;
  return (
    <button
      type="button"
      onClick={() => onExit(row)}
      disabled={!row.id || exiting}
      className={`${mobile ? 'mt-3 w-full' : ''} min-h-9 rounded border border-[#ef4444]/70 px-2.5 py-1.5 text-xs font-semibold text-[#ef4444] transition hover:bg-[#ef4444]/10 disabled:cursor-not-allowed disabled:opacity-50`}
      title={tradingMode === 'live' ? 'Close this LIVE position via Fyers market order' : 'Close this paper position at the latest Fyers price'}
    >
      <i className="ri-close-circle-fill mr-1 text-sm" />
      {exiting ? 'Exiting...' : 'Exit'}
    </button>
  );
}

function PositionSourceBadge({ row }: { row: any }) {
  const fromFyersOrder = row.is_broker_order || row.position_source === 'fyers_order';
  const fromFyersApp = row.is_broker_position || row.position_source === 'fyers_app';
  return (
    <span className={`inline-flex items-center whitespace-nowrap rounded border px-2 py-1 text-[10px] font-semibold uppercase tracking-wide ${
      fromFyersOrder || fromFyersApp
        ? 'border-[#60a5fa]/40 bg-[#3b82f6]/10 text-[#60a5fa]'
        : 'border-[#22c55e]/30 bg-[#22c55e]/10 text-[#22c55e]'
    }`}>
      <i className={`${fromFyersOrder ? 'ri-time-fill' : fromFyersApp ? 'ri-smartphone-fill' : 'ri-robot-2-fill'} mr-1 text-xs`} />
      {fromFyersOrder ? 'FYERS Order' : fromFyersApp ? 'FYERS App' : 'Algorithm'}
    </span>
  );
}

function MobileField({ label, value, wide = false }: { label: string; value: any; wide?: boolean }) {
  return (
    <div className={wide ? 'col-span-2' : ''}>
      <div className="label text-[10px]">{label}</div>
      <div className="num mt-0.5 text-gray-100">{value}</div>
    </div>
  );
}

function SignalAudit({ row }: { row: any }) {
  if (row.is_broker_order || row.position_source === 'fyers_order') {
    return <span className="text-xs text-[#60a5fa]">Pending or scheduled in FYERS ({row.status || 'waiting'})</span>;
  }
  if (row.is_broker_position || row.position_source === 'fyers_app') {
    return <span className="text-xs text-[#60a5fa]">Opened outside the algorithm in FYERS</span>;
  }
  const signal = row.signal_snapshot;
  if (!signal || typeof signal !== 'object') {
    return <span className="text-xs text-gray-500">Not captured for this legacy trade</span>;
  }
  const shape = signal.shape === 'open_equals_low' ? 'BUY: signal open ≈ low (tick tolerance)'
    : signal.shape === 'open_equals_high' ? 'SELL: signal open ≈ high (tick tolerance)'
      : signal.shape === 'flat_ambiguous' ? 'Rejected: flat/ambiguous signal'
        : 'Signal window audit';
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-[#60a5fa]">View signal OHLC</summary>
      <div className="mt-1 space-y-0.5 text-gray-400">
        <div className="font-semibold text-gray-200">{shape}</div>
        <div>{signal.window || 'Opening window'}: O {formatNumber(signal.open)} / H {formatNumber(signal.high)} / L {formatNumber(signal.low)} / C {formatNumber(signal.close)}</div>
        <div>Prev close {formatNumber(signal.previous_close)} | Gap {Number.isFinite(Number(signal.gap_pct)) ? `${Number(signal.gap_pct).toFixed(2)}%` : '--'}</div>
        <div>Entry LTP {formatNumber(signal.entry_ltp)}</div>
      </div>
    </details>
  );
}

// Compact per-row indicator of trailing-SL activity. Reads the metadata
// stamped into signal_snapshot by paper_broker.apply_trailing_stop.
function TrailingBadge({ row }: { row: any }) {
  const snap = row?.signal_snapshot;
  if (!snap || typeof snap !== 'object') {
    return <span className="text-xs text-gray-500">--</span>;
  }
  const trailing = snap.trailing;
  const activated = !!(trailing && trailing.activated);
  const initialSl = Number(snap.initial_sl_price);
  const currentSl = Number(row?.sl_price);
  const side = String(row?.side || '').toUpperCase();

  if (!activated) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-gray-500">
        <span className="h-1.5 w-1.5 rounded-full bg-gray-600" />
        OFF
      </span>
    );
  }

  // Delta relative to the initial SL. For BUY exits the trailed SL rises
  // (positive delta = protection tightened). For SELL exits it falls
  // (delta shown as negative movement in absolute terms).
  const delta = Number.isFinite(initialSl) && Number.isFinite(currentSl)
    ? (side === 'SELL' ? initialSl - currentSl : currentSl - initialSl)
    : null;
  const deltaLabel = delta === null
    ? ''
    : ` (${delta >= 0 ? '+' : ''}${delta.toFixed(2)})`;
  const arrow = side === 'SELL' ? '↓' : '↑';
  const firstAt = trailing?.first_activated_at
    ? new Date(trailing.first_activated_at).toLocaleTimeString('en-IN', {
        hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata',
      })
    : null;
  const bumps = Number(trailing?.update_count) || 0;

  return (
    <div className="text-xs text-gray-300">
      <div className="flex items-center gap-1 font-semibold text-[#22c55e]">
        <span className="h-1.5 w-1.5 rounded-full bg-[#22c55e]" />
        {arrow} {Number.isFinite(currentSl) ? currentSl.toFixed(2) : '--'}
        <span className="text-gray-400">{deltaLabel}</span>
      </div>
      <div className="mt-0.5 text-[10px] text-gray-500">
        {firstAt ? `active ${firstAt}` : 'active'} · {bumps}x{Number.isFinite(initialSl) ? ` · init ${initialSl.toFixed(2)}` : ''}
      </div>
    </div>
  );
}

export function Table({ rows, columns }: { rows: any[]; columns: string[] }) {
  const [page, setPage] = useState(0);
  const safePage = Math.min(page, Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1));
  const visibleRows = rows.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);
  if (!rows.length) return <p className="text-sm text-gray-500">Nothing here yet.</p>;
  return (
    <div className="mb-6">
      <div className="overflow-x-auto rounded border border-[#1f2937]">
      <table className="w-full min-w-max border-collapse text-sm">
        <thead className="bg-[#111827]">
          <tr>{columns.map((c) => (
            <th key={c} className="table-cell label">{c}</th>
          ))}</tr>
        </thead>
        <tbody>
          {visibleRows.map((row, i) => (
            <tr key={i} className={i % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
              {columns.map((c) => (
                <td key={c} className="table-cell text-gray-100">{String(row[c] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <PaginationControls page={safePage} totalRows={rows.length} onPageChange={setPage} />
    </div>
  );
}

function formatMoney(value: unknown) {
  const number = Number(value || 0);
  return `Rs ${number.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function optionalNumber(...values: unknown[]) {
  for (const value of values) {
    if (value === null || value === undefined || value === '') continue;
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function calculateUnrealized(position: any, ltpValue: unknown) {
  const ltp = Number(ltpValue);
  const entry = Number(position.entry_price || 0);
  const qty = Number(position.qty || 0);
  if (!Number.isFinite(ltp) || !entry || !qty) return position.unrealized_pnl ?? 0;
  return (position.side === 'SELL' ? entry - ltp : ltp - entry) * qty;
}

function formatSignedMoney(value: number) {
  const sign = value > 0 ? '+' : '';
  return `${sign}${formatMoney(value)}`;
}

function formatDateTime(value: unknown) {
  if (!value) return '--';
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function formatNumber(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return number.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function pnlColor(value?: number | null) {
  if (value === undefined || value === null) return 'text-gray-100';
  if (value > 0) return 'text-[#22c55e]';
  if (value < 0) return 'text-[#ef4444]';
  return 'text-gray-100';
}

function reasonColor(reason: string) {
  if (reason === 'TARGET') return 'text-[#22c55e]';
  if (reason === 'SL') return 'text-[#ef4444]';
  if (reason === 'EOD_SQUAREOFF' || reason === 'EOD') return 'text-[#f59e0b]';
  return 'text-gray-100';
}

function reasonIcon(reason: string) {
  if (reason === 'TARGET') return <i className="ri-checkbox-circle-fill mr-1 text-sm text-[#22c55e]" />;
  if (reason === 'SL') return <i className="ri-close-circle-fill mr-1 text-sm text-[#ef4444]" />;
  if (reason === 'EOD_SQUAREOFF' || reason === 'EOD') return <i className="ri-error-warning-fill mr-1 text-sm text-[#f59e0b]" />;
  return null;
}

function formatReason(reason: string) {
  if (reason === 'EOD_SQUAREOFF') return 'EOD';
  return reason || '--';
}

function formatTrigger(trigger: unknown) {
  const value = String(trigger || '').trim();
  return value || 'Legacy row: trigger was not stored when this trade opened.';
}
