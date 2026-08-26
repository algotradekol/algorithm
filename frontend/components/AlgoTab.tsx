'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  const [brokerPnlSummary, setBrokerPnlSummary] = useState<any>(null);
  const [brokerPositionsError, setBrokerPositionsError] = useState('');
  const [brokerOrders, setBrokerOrders] = useState<any[]>([]);
  const [brokerOrdersError, setBrokerOrdersError] = useState('');
  const [error, setError] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [exitingPositionId, setExitingPositionId] = useState<string | null>(null);
  const [editingProtection, setEditingProtection] = useState<any | null>(null);
  const [savingProtection, setSavingProtection] = useState(false);
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
      setBrokerPnlSummary(null);
      setBrokerPositionsError('');
      return;
    }
    try {
      const result = await api.fyersPositions(tradingMode);
      if (requestId !== brokerPositionsRequestId.current) return;
      if (result?.available !== false) {
        setBrokerPositions(Array.isArray(result?.positions) ? result.positions : []);
        setBrokerPnlSummary(result?.pnl_summary || null);
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
    setBrokerPnlSummary(null);
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

  const openEditProtection = useCallback((position: any) => {
    if (!position?.id) {
      setError('This legacy position has no ID and cannot be edited from the dashboard.');
      return;
    }
    setError('');
    setEditingProtection(position);
  }, []);

  const submitEditProtection = useCallback(async (payload: { sl_price?: number; target_price?: number }) => {
    if (!editingProtection?.id) return;
    setSavingProtection(true);
    setError('');
    try {
      const result: any = await api.updatePositionProtection(
        algoId,
        String(editingProtection.id),
        payload,
      );
      const updatedFields: Record<string, any> = {};
      if (typeof result?.sl_price === 'number') updatedFields.sl_price = result.sl_price;
      if (typeof result?.target_price === 'number') updatedFields.target_price = result.target_price;
      setPositions((current) => current.map((row) => (
        String(row.id) === String(editingProtection.id)
          ? { ...row, ...updatedFields }
          : row
      )));
      setEditingProtection(null);
    } catch (editError: any) {
      setError(editError?.message || 'Could not update SL / Target.');
    } finally {
      setSavingProtection(false);
    }
  }, [algoId, editingProtection]);

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
        {algoId !== 'algo3' && (
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
        )}
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
  const isSilverAlgo = algoId === 'algo3';
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
  const fyersTotalPnl = optionalNumber(brokerPnlSummary?.total_pnl);
  const useFyersLivePnl = tradingMode === 'live'
    && fyersConnected === true
    && brokerPnlSummary !== null
    && fyersTotalPnl !== null;
  const displayedGrossPnl = grossPnl;
  const displayedNetPnl = tradingMode === 'live'
    ? (useFyersLivePnl ? fyersTotalPnl : netPnl)
    : netPnl;
  const grossPnlHelper = tradingMode === 'paper'
    ? 'Closed paper trades only'
    : 'Closed live trades only';
  const netPnlHelper = useFyersLivePnl
    ? `source: FYERS ${brokerPnlSummary?.source === 'fyers_overall' ? 'overall positions' : 'open positions'}`
    : tradingMode === 'live'
      ? (brokerPositionsError || 'FYERS live P&L unavailable, showing closed-trade net only')
      : 'Closed paper trades only';
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
    <section className="min-w-0 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-gray-100">{displayName}</h2>
          {description && <p className="mt-1 text-xs text-gray-500">{description}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ScanToggleButton
            algoId={algoId}
            enabled={summary?.scan_enabled !== false}
            onChange={(next) => setSummary((prev: any) => (prev ? { ...prev, scan_enabled: next } : prev))}
          />
          <TradingToggleButton
            algoId={algoId}
            enabled={summary?.trading_enabled !== false}
            onChange={(next) => setSummary((prev: any) => (prev ? { ...prev, trading_enabled: next } : prev))}
          />
          <button
            onClick={() => setSettingsOpen((open) => !open)}
            className="min-h-10 rounded border border-[#3b82f6] px-3 py-1.5 text-xs font-semibold text-[#3b82f6]"
          >
            Settings
          </button>
        </div>
      </div>
      {summary && summary.scan_enabled === false && (
        <p className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">
          Scan is OFF for this strategy. No entries will be evaluated until you turn it back ON above.
        </p>
      )}
      {summary && summary.trading_enabled === false && (
        <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">
          Trading is OFF for this strategy. Scan and setups are still running, and open positions are still managed — but no NEW entries (including reversals) will be submitted until you turn it back ON above.
        </p>
      )}
      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}
      {/* F12: the three /api/fyers/* calls (funds, positions, orders) share
          the same warning string during a 429 cooldown — the old rendering
          stacked three identical banners. Deduplicate by unique text. */}
      {(() => {
        if (tradingMode !== 'live' || !fyersConnected) return null;
        const seen = new Set<string>();
        const messages = [walletStatusError, brokerPositionsError, brokerOrdersError]
          .filter((msg): msg is string => Boolean(msg) && !seen.has(msg) && (seen.add(msg), true));
        return messages.map((msg, i) => (
          <p key={i} className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">
            {msg}
          </p>
        ));
      })()}
      {isSilverAlgo && <SilverFeedPanel status={feedStatus} />}

      {isSilverAlgo ? (
        <div className="grid grid-cols-2 gap-1.5 sm:gap-2 lg:grid-cols-4">
          <MetricCard
            label="Total Capital"
            value={formatMoney(totalCapital)}
            delta={formatSignedMoney(totalCapital - startingCapital)}
            pnl={totalCapital - startingCapital}
          />
          <MetricCard
            label="Trades Today"
            value={String(summary.trade_count_today ?? 0)}
            helper={`${summary.buy_count_today ?? 0} buy / ${summary.sell_count_today ?? 0} sell`}
          />
          <MetricCard
            label="Realized Gross P&L"
            value={formatMoney(displayedGrossPnl)}
            pnl={displayedGrossPnl}
            helper={grossPnlHelper}
          />
          <MetricCard
            label={useFyersLivePnl ? 'FYERS Live P&L' : 'Net P&L'}
            value={formatMoney(displayedNetPnl)}
            pnl={displayedNetPnl}
            helper={netPnlHelper}
            important
          />
        </div>
      ) : (
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
          <MetricCard
            label="Realized Gross P&L"
            value={formatMoney(displayedGrossPnl)}
            pnl={displayedGrossPnl}
            helper={grossPnlHelper}
          />
          <MetricCard
            label={useFyersLivePnl ? 'FYERS Live P&L' : 'Net P&L'}
            value={formatMoney(displayedNetPnl)}
            pnl={displayedNetPnl}
            helper={netPnlHelper}
            important
          />
        </div>
      )}

      <SettingsDrawer open={settingsOpen} algoId={algoId} tradingMode={tradingMode} onClose={() => setSettingsOpen(false)} />

      {editingProtection && (
        <EditProtectionDialog
          position={editingProtection}
          saving={savingProtection}
          tradingMode={tradingMode}
          onClose={() => setEditingProtection(null)}
          onSubmit={submitEditProtection}
        />
      )}

      {algoId !== 'algo3' && (
        <ScanResultsPanel algoId={algoId} results={scanResults} openPositions={positions} onRefresh={loadData} />
      )}

      <div className="grid min-w-0 gap-4">
        <section className="min-w-0">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">Open Positions</h3>
          <PositionsTable rows={openPositionRows} onExit={exitPosition} onEditProtection={openEditProtection} exitingPositionId={exitingPositionId} tradingMode={tradingMode} />
        </section>

        <section className="min-w-0">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">Closed Trades Today</h3>
          <TradesTable rows={trades} />
        </section>
      </div>
      {description && <div className="rounded border border-[#1f2937] bg-[#111827] px-3 py-2 text-xs text-gray-500">{description}</div>}
    </section>
  );
}

function SilverFeedPanel({ status }: { status: any }) {
  const [historyOpenSide, setHistoryOpenSide] = useState<'BUY' | 'SELL' | null>(null);
  const [historyRows, setHistoryRows] = useState<any[]>([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [historySelectedDate, setHistorySelectedDate] = useState<string | null>(null);
  // All five feed timestamps use the with-date formatter so it's obvious
  // whether "16:15" is today's tick or yesterday's stale warmup value.
  const lastTick = formatDateTimeWithDate(status?.last_tick_at);
  const lastMinuteCandle = formatDateTimeWithDate(status?.last_minute_candle_at);
  const lastBar = formatDateTimeWithDate(status?.last_bar_at);
  const buySetupAt = formatDateTimeWithDate(status?.buy_setup_bar_at);
  const sellSetupAt = formatDateTimeWithDate(status?.sell_setup_bar_at);
  const historyBadge = status?.history_loading
    ? 'loading history'
    : status?.history_ready
    ? 'history ready'
    : status?.history_error
    ? 'history error'
    : 'history pending';
  const buySetupClose = status?.buy_setup_close;
  const sellSetupClose = status?.sell_setup_close;
  const n = status?.n_points ?? 150;
  const buyTrigger = buySetupClose != null ? Number(buySetupClose) + Number(n) : null;
  const sellTrigger = sellSetupClose != null ? Number(sellSetupClose) - Number(n) : null;
  const historyGroups = useMemo(() => buildSetupHistoryGroups(historyRows), [historyRows]);
  const activeHistoryGroup = useMemo(() => {
    if (!historyGroups.length) return null;
    const explicit = historySelectedDate
      ? historyGroups.find((group) => group.dateKey === historySelectedDate)
      : null;
    return explicit || historyGroups[0];
  }, [historyGroups, historySelectedDate]);
  async function openHistory(side: 'BUY' | 'SELL') {
    setHistoryOpenSide(side);
    setHistoryBusy(true);
    setHistoryError('');
    setHistorySelectedDate(null);
    try {
      const result = await api.setupHistory('algo3', side, 30, 100, {
        currentSessionOnly: true,
        liveOnly: true,
      });
      const nextRows = Array.isArray(result?.rows) ? result.rows : [];
      setHistoryRows(nextRows);
      const nextGroups = buildSetupHistoryGroups(nextRows);
      setHistorySelectedDate(nextGroups[0]?.dateKey || null);
      setHistoryError(result?.warning || '');
    } catch (e: any) {
      setHistoryRows([]);
      setHistorySelectedDate(null);
      setHistoryError(e?.message || 'Failed to load setup history');
    } finally {
      setHistoryBusy(false);
    }
  }
  return (
    <div className="rounded border border-[#3b82f6]/30 bg-[#0b1220] p-3 text-xs text-gray-300">
      <div className="flex items-center justify-between gap-3">
        <div className="label text-[10px]">Silver feed diagnostics (15m EMA breakout)</div>
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
        <FeedStat label="Last 15m bar" value={lastBar || '--'} />
        <FeedStat label="15m bars stored" value={status?.bars_15m ?? 0} />
        <FeedStat label="EMA20" value={formatNumber(status?.ema20)} />
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="rounded border border-[#22c55e]/30 bg-[#22c55e]/5 p-2">
          <div className="flex items-center justify-between gap-2">
            <div className="label text-[10px] text-[#22c55e]">BUY setup (green &gt; EMA20)</div>
            <button
              onClick={() => openHistory('BUY')}
              className="rounded border border-[#22c55e]/40 px-2 py-0.5 text-[10px] font-semibold text-[#22c55e]"
            >
              History
            </button>
          </div>
          <div className="mt-1 num text-sm text-gray-100">
            {buySetupClose != null ? `Close ${formatNumber(buySetupClose)}` : 'None captured yet'}
          </div>
          <div className="text-[10px] text-gray-500">
            {buyTrigger != null ? `Fires on tick >= ${formatNumber(buyTrigger)} (setup + ${n})` : `Waiting for a green 15m candle to close above EMA20`}
            {buySetupAt && <span className="ml-1">| set at {buySetupAt}</span>}
          </div>
        </div>
        <div className="rounded border border-[#ef4444]/30 bg-[#ef4444]/5 p-2">
          <div className="flex items-center justify-between gap-2">
            <div className="label text-[10px] text-[#ef4444]">SELL setup (red &lt; EMA20)</div>
            <button
              onClick={() => openHistory('SELL')}
              className="rounded border border-[#ef4444]/40 px-2 py-0.5 text-[10px] font-semibold text-[#ef4444]"
            >
              History
            </button>
          </div>
          <div className="mt-1 num text-sm text-gray-100">
            {sellSetupClose != null ? `Close ${formatNumber(sellSetupClose)}` : 'None captured yet'}
          </div>
          <div className="text-[10px] text-gray-500">
            {sellTrigger != null ? `Fires on tick <= ${formatNumber(sellTrigger)} (setup - ${n})` : `Waiting for a red 15m candle to close below EMA20`}
            {sellSetupAt && <span className="ml-1">| set at {sellSetupAt}</span>}
          </div>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-[10px] text-gray-500">
        <span>History load: {status?.history_error || status?.history_loading ? 'check logs' : 'ok'}</span>
        <span>Warmup 1m candles: {status?.warmup_minute_candles ?? 0}</span>
        <span>n (breakout offset): {n}</span>
      </div>
      {historyOpenSide && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4">
          <div className="flex max-h-[80vh] w-full max-w-5xl flex-col overflow-hidden rounded border border-[#1f2937] bg-[#0d1117] shadow-2xl">
            <div className="flex shrink-0 items-center justify-between gap-3 border-b border-[#1f2937] px-4 py-3">
              <div>
                <div className="text-sm font-semibold text-gray-100">{historyOpenSide} setup history</div>
                <div className="text-xs text-gray-500">
                  Saved qualifying 15m candles for Silver Micro
                </div>
              </div>
              <button onClick={() => setHistoryOpenSide(null)} className="text-sm text-gray-500 hover:text-gray-100">X</button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              {historyError && (
                <p className="mb-3 rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-xs text-[#f59e0b]">
                  {historyError}
                </p>
              )}
              {historyBusy ? (
                <p className="text-sm text-gray-400">Loading setup history...</p>
              ) : !historyRows.length ? (
                <p className="text-sm text-gray-400">No saved {historyOpenSide.toLowerCase()} setup candles yet.</p>
              ) : (
                <div className="space-y-4">
                  <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <div className="text-xs font-semibold text-gray-200">Grouped by trading date</div>
                        <div className="text-[11px] text-gray-500">Pick a day to inspect only that session's saved setup candles.</div>
                      </div>
                      <div className="text-[11px] text-gray-500">{historyRows.length} setup rows loaded</div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {historyGroups.map((group) => {
                        const selected = activeHistoryGroup?.dateKey === group.dateKey;
                        return (
                          <button
                            key={group.dateKey}
                            type="button"
                            onClick={() => setHistorySelectedDate(group.dateKey)}
                            className={`rounded border px-3 py-2 text-left transition ${
                              selected
                                ? historyOpenSide === 'BUY'
                                  ? 'border-[#22c55e]/60 bg-[#22c55e]/10 text-[#22c55e]'
                                  : 'border-[#ef4444]/60 bg-[#ef4444]/10 text-[#ef4444]'
                                : 'border-[#1f2937] bg-[#0d1117] text-gray-300 hover:border-[#334155]'
                            }`}
                          >
                            <div className="text-xs font-semibold">{group.dateLabel}</div>
                            <div className="mt-0.5 text-[11px] text-gray-500">{group.rows.length} candle{group.rows.length === 1 ? '' : 's'}</div>
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {activeHistoryGroup && (
                    <>
                      <div className="max-h-[48vh] overflow-auto rounded border border-[#1f2937]">
                        <table className="w-full min-w-[440px] border-collapse text-xs">
                          <thead className="bg-[#111827]">
                            <tr>
                              {['Time', 'Close', 'Target'].map((column) => (
                                <th key={column} className="table-cell label">{column}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {activeHistoryGroup.rows.map((row, index) => (
                              <tr key={`${row.setup_side}-${row.candle_time}-${index}`} className={index % 2 === 0 ? 'bg-[#0d1117]' : 'bg-[#111827]'}>
                                <td className="table-cell text-gray-300">{formatDateTimeWithDate(row.candle_time)}</td>
                                <td className="table-cell num text-gray-100">{formatNumber(row.candle_close)}</td>
                                <td className="table-cell num text-gray-100">{formatNumber(row.trigger_level)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ScanToggleButton({
  algoId,
  enabled,
  onChange,
}: {
  algoId: string;
  enabled: boolean;
  onChange: (next: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function toggle() {
    if (busy) return;
    const next = !enabled;
    // Confirm only when turning OFF — flipping back ON is harmless.
    if (!next && !window.confirm('Turn scan OFF for this strategy? No entries will be evaluated until you turn it back ON — this setting persists across restarts and days.')) {
      return;
    }
    setBusy(true);
    setError('');
    try {
      const res = await api.setScanEnabled(algoId, next);
      onChange(res?.scan_enabled !== false);
    } catch (e: any) {
      setError(e?.message || 'Failed to update scan state');
    } finally {
      setBusy(false);
    }
  }
  const tone = enabled
    ? 'border-[#22c55e] text-[#22c55e]'
    : 'border-[#f59e0b] text-[#f59e0b]';
  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={toggle}
        disabled={busy}
        title={enabled
          ? 'Scan is ON. Click to turn it OFF (persists until re-enabled).'
          : 'Scan is OFF. Click to turn it back ON.'}
        className={`min-h-10 rounded border px-3 py-1.5 text-xs font-semibold disabled:cursor-wait ${tone}`}
      >
        {busy ? '...' : enabled ? 'Scan: ON' : 'Scan: OFF'}
      </button>
      {error && <p className="m-0 max-w-xs text-right text-[10px] text-[#ef4444]">{error}</p>}
    </div>
  );
}

function TradingToggleButton({
  algoId,
  enabled,
  onChange,
}: {
  algoId: string;
  enabled: boolean;
  onChange: (next: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function toggle() {
    if (busy) return;
    const next = !enabled;
    // Confirm only when turning OFF — flipping back ON is harmless.
    if (!next && !window.confirm(
      'Turn TRADING OFF for this strategy? Scan and setups keep running and open positions are still managed, but no NEW entries (including reversals) will be submitted until you turn it back ON. Persists across restarts.'
    )) {
      return;
    }
    setBusy(true);
    setError('');
    try {
      const res = await api.setTradingEnabled(algoId, next);
      onChange(res?.trading_enabled !== false);
    } catch (e: any) {
      setError(e?.message || 'Failed to update trading state');
    } finally {
      setBusy(false);
    }
  }
  const tone = enabled
    ? 'border-[#22c55e] text-[#22c55e]'
    : 'border-[#ef4444] text-[#ef4444]';
  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={toggle}
        disabled={busy}
        title={enabled
          ? 'Trading is ON. Click to turn it OFF — scan keeps running but no new entries will be submitted.'
          : 'Trading is OFF. Scan is still running; click to turn trading back ON.'}
        className={`min-h-10 rounded border px-3 py-1.5 text-xs font-semibold disabled:cursor-wait ${tone}`}
      >
        {busy ? '...' : enabled ? 'Trading: ON' : 'Trading: OFF'}
      </button>
      {error && <p className="m-0 max-w-xs text-right text-[10px] text-[#ef4444]">{error}</p>}
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
  onEditProtection,
  exitingPositionId,
  tradingMode,
}: {
  rows: any[];
  onExit: (row: any) => void;
  onEditProtection: (row: any) => void;
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
                <MobileField label="SL" value={formatNumber(row.sl_price)} />
                <MobileField label="Target" value={formatNumber(row.target_price)} />
                <MobileField label="Trailing SL" value={<TrailingBadge row={row} />} wide />
                <MobileField label="Trigger" value={formatTrigger(row.entry_trigger)} wide />
                <MobileField label="Signal Audit" value={<SignalAudit row={row} />} wide />
              </div>
              <div className="mt-3 flex w-full gap-2">
                <EditProtectionButton row={row} onEdit={onEditProtection} tradingMode={tradingMode} mobile />
                <ManualExitButton row={row} onExit={onExit} exitingPositionId={exitingPositionId} tradingMode={tradingMode} mobile /></div>
            </div>
          );
        })}
      </div>
      <div className="hidden w-full max-w-full overflow-x-auto overscroll-x-contain rounded border border-[#1f2937] sm:block">
        <table className="w-full min-w-[1550px] table-auto border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {['#', 'Symbol', 'Source', 'Side', 'Qty', 'Entry Time', 'Entry', 'LTP', 'SL', 'Target', 'Trailing SL', 'Signal Audit', 'Trigger', 'Unreal P&L', 'Exit'].map((column) => (
              <th key={column} className="table-cell label whitespace-nowrap">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={15} className="table-cell text-gray-500">No open positions</td>
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
              <tr key={row.id || index} className={`align-top ${index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}`}>
                <td className="table-cell num whitespace-nowrap text-gray-500">{safePage * PAGE_SIZE + index + 1}</td>
                <td className="table-cell w-[180px] whitespace-nowrap font-mono text-gray-100">{row.symbol}</td>
                <td className="table-cell w-[120px] whitespace-nowrap"><PositionSourceBadge row={row} /></td>
                <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                  <i className={`${row.side === 'SELL' ? 'ri-indeterminate-circle-fill' : 'ri-add-circle-fill'} mr-1 text-sm`} />
                  {row.side === 'SELL' ? 'S' : 'B'}
                </td>
                <td className="table-cell num whitespace-nowrap text-gray-100">{row.qty}</td>
                <td className="table-cell num whitespace-nowrap text-gray-400">{formatDateTime(row.entry_time)}</td>
                <td className="table-cell num whitespace-nowrap text-gray-100">{formatNumber(row.entry_price)}</td>
                <td className="table-cell num whitespace-nowrap text-gray-100">{Number.isFinite(ltp) ? formatNumber(ltp) : '--'}</td>
                <td className="table-cell num whitespace-nowrap text-gray-100">{formatNumber(row.sl_price)}</td>
                <td className="table-cell num whitespace-nowrap text-gray-100">{formatNumber(row.target_price)}</td>
                <td className="table-cell w-[170px] whitespace-nowrap"><TrailingBadge row={row} /></td>
                <td className="table-cell w-[260px] max-w-[320px] text-gray-400 align-top">
                  <div className="max-h-24 overflow-y-auto break-words whitespace-normal pr-1 leading-relaxed">
                    <SignalAudit row={row} />
                  </div>
                </td>
                <td className="table-cell w-[340px] max-w-[420px] text-gray-400 align-top">
                  <div className="max-h-24 overflow-y-auto break-words whitespace-normal pr-1 leading-relaxed">
                    {formatTrigger(row.entry_trigger)}
                  </div>
                </td>
                <td className={`table-cell w-[120px] num whitespace-nowrap font-semibold ${pnlColor(unreal)}`}>{unreal === null ? '--' : formatMoney(unreal)}</td>
                <td className="table-cell w-[150px] whitespace-nowrap">
                  <div className="flex items-center gap-1.5">
                    <EditProtectionButton row={row} onEdit={onEditProtection} tradingMode={tradingMode} />
                    <ManualExitButton row={row} onExit={onExit} exitingPositionId={exitingPositionId} tradingMode={tradingMode} />
                  </div>
                </td>
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

function formatTradeQty(row: any): string {
  const qty = Number(row?.qty ?? 0);
  if (!Number.isFinite(qty) || qty <= 0) return '--';
  const symbol = String(row?.symbol || '').toUpperCase();
  // Silver Micro is sized in lots (1 lot = 1 kg = 1 unit on Fyers). NSE
  // instruments are sized in raw share quantity. Show the correct label so
  // 1 kg of silver does not read like 1 share of equity.
  const isSilverMicro = symbol.startsWith('MCX:SILVERMIC');
  if (isSilverMicro) {
    return `${qty} lot${qty === 1 ? '' : 's'}`;
  }
  return `${qty} qty`;
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
              <MobileField label="Qty / Lots" value={formatTradeQty(row)} />
              <MobileField label="Entry Time" value={formatTradeTime(row.entry_time, row.exit_time)} />
              <MobileField label="Entry" value={formatNumber(row.entry_price)} />
              <MobileField label="Exit Time" value={row.exit_time ? formatTradeTime(row.exit_time, row.entry_time) : "--"} />
              <MobileField label="Exit" value={formatNumber(row.exit_price)} />
              <MobileField label="Reason" value={formatReason(row.exit_reason)} />
              <MobileField label="Trailing SL" value={<TrailingBadge row={row} />} wide />
              <MobileField label="Gross" value={formatMoney(row.gross_pnl)} />
              <MobileField label="Charges" value={formatMoney(row.total_charges)} />
            </div>
          </div>
        ))}
      </div>
      <div className="hidden w-full max-w-full overflow-x-auto overscroll-x-contain rounded border border-[#1f2937] sm:block">
        <table className="w-full min-w-[1680px] table-auto border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {["Symbol", "Side", "Qty / Lots", "Entry Time", "Entry", "Exit Time", "Exit", "Reason", "Trailing SL", "Signal Audit", "Trigger", "Gross", "Charges", "Net"].map((column) => (
              <th key={column} className="table-cell label whitespace-nowrap">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={14} className="table-cell text-gray-500">No closed trades yet</td>
            </tr>
          ) : visibleRows.map((row, index) => (
            <tr key={row.id || index} className={`align-top ${index % 2 === 0 ? "bg-[#111827]" : "bg-[#0d1117]"}`}>
              <td className="table-cell w-[180px] whitespace-nowrap font-mono text-gray-100">{row.symbol}</td>
              <td className={`table-cell font-semibold ${row.side === "SELL" ? "text-[#ef4444]" : "text-[#22c55e]"}`}>
                <i className={`${row.side === "SELL" ? "ri-indeterminate-circle-fill" : "ri-add-circle-fill"} mr-1 text-sm`} />
                {row.side === "SELL" ? "S" : "B"}
              </td>
              <td className="table-cell num whitespace-nowrap text-gray-100">{formatTradeQty(row)}</td>
              <td className="table-cell num whitespace-nowrap text-gray-400">{formatTradeTime(row.entry_time, row.exit_time)}</td>
              <td className="table-cell num whitespace-nowrap text-gray-100">{formatNumber(row.entry_price)}</td>
              <td className="table-cell num whitespace-nowrap text-gray-400">{row.exit_time ? formatTradeTime(row.exit_time, row.entry_time) : "--"}</td>
              <td className="table-cell num whitespace-nowrap text-gray-100">{formatNumber(row.exit_price)}</td>
              <td className={`table-cell font-semibold ${reasonColor(row.exit_reason)}`}>
                {reasonIcon(row.exit_reason)}
                {formatReason(row.exit_reason)}
              </td>
              <td className="table-cell w-[170px] whitespace-nowrap"><TrailingBadge row={row} /></td>
              <td className="table-cell w-[260px] max-w-[320px] text-gray-400 align-top"><div className="max-h-24 overflow-y-auto break-words whitespace-normal leading-relaxed"><SignalAudit row={row} /></div></td>
              <td className="table-cell w-[340px] max-w-[420px] break-words whitespace-normal text-gray-400 align-top leading-relaxed">{formatTrigger(row.entry_trigger)}</td>
              <td className={`table-cell w-[110px] num whitespace-nowrap ${pnlColor(Number(row.gross_pnl || 0))}`}>{formatMoney(row.gross_pnl)}</td>
              <td className="table-cell w-[110px] num whitespace-nowrap text-gray-100">{formatMoney(row.total_charges)}</td>
              <td className={`table-cell w-[120px] num whitespace-nowrap font-semibold ${pnlColor(Number(row.net_pnl || 0))}`}>{formatMoney(row.net_pnl)}</td>
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

function EditProtectionButton({
  row,
  onEdit,
  tradingMode,
  mobile = false,
}: {
  row: any;
  onEdit: (row: any) => void;
  tradingMode?: string;
  mobile?: boolean;
}) {
  const isBrokerRow = row.is_broker_order
    || row.position_source === 'fyers_order'
    || row.is_broker_position
    || row.position_source === 'fyers_app';
  if (isBrokerRow) return null;
  const title = tradingMode === 'live'
    ? 'Edit SL / Target — pushed to Fyers immediately'
    : 'Edit SL / Target for this paper position';
  return (
    <button
      type="button"
      onClick={() => onEdit(row)}
      disabled={!row.id}
      className={`${mobile ? 'flex-1' : ''} min-h-9 rounded border border-[#3b82f6]/70 px-2.5 py-1.5 text-xs font-semibold text-[#3b82f6] transition hover:bg-[#3b82f6]/10 disabled:cursor-not-allowed disabled:opacity-50`}
      title={title}
    >
      <i className="ri-edit-line mr-1 text-sm" />
      Edit
    </button>
  );
}

function EditProtectionDialog({
  position,
  saving,
  tradingMode,
  onClose,
  onSubmit,
}: {
  position: any;
  saving: boolean;
  tradingMode?: string;
  onClose: () => void;
  onSubmit: (payload: { sl_price?: number; target_price?: number }) => void;
}) {
  const [slInput, setSlInput] = useState('');
  const [targetInput, setTargetInput] = useState('');
  const [localError, setLocalError] = useState('');

  useEffect(() => {
    if (position) {
      const sl = Number(position.sl_price);
      const target = Number(position.target_price);
      setSlInput(Number.isFinite(sl) && sl > 0 ? String(sl) : '');
      setTargetInput(Number.isFinite(target) && target > 0 ? String(target) : '');
      setLocalError('');
    }
  }, [position]);

  if (!position) return null;

  const side = String(position.side || '').toUpperCase();
  const entry = Number(position.entry_price || 0);
  const currentSl = Number(position.sl_price || 0);
  const currentTarget = Number(position.target_price || 0);

  function handleSubmit(event: any) {
    event.preventDefault();
    const payload: { sl_price?: number; target_price?: number } = {};
    const slNumber = slInput.trim() === '' ? NaN : Number(slInput);
    const targetNumber = targetInput.trim() === '' ? NaN : Number(targetInput);
    if (Number.isFinite(slNumber) && slNumber !== currentSl) {
      if (slNumber <= 0) { setLocalError('Stop loss must be greater than zero.'); return; }
      if (side === 'BUY' && entry && slNumber >= entry) { setLocalError('Stop loss for a BUY must be below entry.'); return; }
      if (side === 'SELL' && entry && slNumber <= entry) { setLocalError('Stop loss for a SELL must be above entry.'); return; }
      payload.sl_price = slNumber;
    }
    if (Number.isFinite(targetNumber) && targetNumber !== currentTarget) {
      if (targetNumber <= 0) { setLocalError('Target must be greater than zero.'); return; }
      if (side === 'BUY' && entry && targetNumber <= entry) { setLocalError('Target for a BUY must be above entry.'); return; }
      if (side === 'SELL' && entry && targetNumber >= entry) { setLocalError('Target for a SELL must be below entry.'); return; }
      payload.target_price = targetNumber;
    }
    if (!Object.keys(payload).length) {
      setLocalError('Change at least one value.');
      return;
    }
    setLocalError('');
    onSubmit(payload);
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true">
      <form onSubmit={handleSubmit} className="w-full max-w-md rounded border border-[#1f2937] bg-[#0d1117] p-4 shadow-lg">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-base font-semibold text-gray-100">Edit SL / Target</h3>
            <p className="mt-1 text-xs text-gray-500">
              {position.symbol} · {side} · Entry {formatNumber(entry)}
            </p>
          </div>
          <button type="button" onClick={onClose} className="text-gray-500 hover:text-gray-300" aria-label="Close">
            <i className="ri-close-line text-lg" />
          </button>
        </div>
        {tradingMode === 'live' && (
          <p className="mt-3 rounded border border-[#3b82f6]/40 bg-[#3b82f6]/10 px-3 py-2 text-xs text-[#93c5fd]">
            Change is pushed to Fyers first. If Fyers rejects, nothing here or in Fyers moves.
          </p>
        )}
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <label>
            <div className="label">Stop Loss</div>
            <input
              type="number"
              step="0.05"
              min="0"
              value={slInput}
              onChange={(e) => setSlInput(e.target.value)}
              className="control mt-1 num"
              disabled={saving}
            />
            <div className="mt-1 text-xs text-gray-500">Current: {formatNumber(currentSl)}</div>
          </label>
          <label>
            <div className="label">Target</div>
            <input
              type="number"
              step="0.05"
              min="0"
              value={targetInput}
              onChange={(e) => setTargetInput(e.target.value)}
              className="control mt-1 num"
              disabled={saving}
            />
            <div className="mt-1 text-xs text-gray-500">Current: {formatNumber(currentTarget)}</div>
          </label>
        </div>
        {localError && <p className="mt-3 rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{localError}</p>}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="min-h-9 rounded border border-[#1f2937] px-3 py-1.5 text-xs font-semibold text-gray-300 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving}
            className="min-h-9 rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-1.5 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </form>
    </div>
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
      <div className="num mt-0.5 break-words whitespace-normal text-gray-100">{value}</div>
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
  const algo3Snapshot = signal.timeframe === '15m' && (
    signal.buy_setup_close !== undefined ||
    signal.sell_setup_close !== undefined ||
    signal.trigger_level !== undefined ||
    signal.ema20 !== undefined
  );
  if (algo3Snapshot) {
    const triggerLabel = signal.side === 'SELL' ? 'Sell trigger' : 'Buy trigger';
    const activeSetup = signal.side === 'SELL' ? signal.sell_setup_close : signal.buy_setup_close;
    return (
      <details className="max-w-full text-xs">
        <summary className="cursor-pointer text-[#60a5fa]">View signal OHLC</summary>
        <div className="mt-1 space-y-0.5 break-words whitespace-normal text-gray-400">
          <div className="font-semibold text-gray-200">15m EMA breakout audit</div>
          <div>Symbol {signal.symbol || row.symbol || '--'} | Side {signal.side || row.side || '--'}</div>
          <div>Setup close {formatNumber(activeSetup)} | {triggerLabel} {formatNumber(signal.trigger_level)}</div>
          <div>Entry LTP {formatNumber(signal.entry_ltp)} | EMA20 {formatNumber(signal.ema20)}</div>
          <div>Buy setup {formatNumber(signal.buy_setup_close)} | Sell setup {formatNumber(signal.sell_setup_close)}</div>
        </div>
      </details>
    );
  }
  const shape = signal.shape === 'open_equals_low' ? 'BUY: signal open ≈ low (tick tolerance)'
    : signal.shape === 'open_equals_high' ? 'SELL: signal open ≈ high (tick tolerance)'
      : signal.shape === 'flat_ambiguous' ? 'Rejected: flat/ambiguous signal'
        : 'Signal window audit';
  return (
    <details className="max-w-full text-xs">
      <summary className="cursor-pointer text-[#60a5fa]">View signal OHLC</summary>
      <div className="mt-1 space-y-0.5 break-words whitespace-normal text-gray-400">
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
  const [historyOpen, setHistoryOpen] = useState(false);
  const snap = row?.signal_snapshot;
  if (!snap || typeof snap !== 'object') {
    return <span className="text-xs text-gray-500">--</span>;
  }
  const trailing = snap.trailing;
  const breakevenPolicy = snap.silver_exit_policy === 'target_to_breakeven_sl';
  const breakeven = snap.silver_breakeven && typeof snap.silver_breakeven === 'object'
    ? snap.silver_breakeven
    : {};
  const activated = !!(trailing && trailing.activated);
  const initialSl = Number(snap.initial_sl_price);
  const side = String(row?.side || '').toUpperCase();
  const events = Array.isArray(trailing?.events)
    ? trailing.events.filter((event: any) => event && typeof event === 'object')
    : [];
  const latestEvent = events.length ? events[events.length - 1] : null;
  const isStopExit = String(row?.exit_reason || '').toUpperCase().includes('SL');
  const currentSl = optionalPositiveNumber(
    latestEvent?.new_sl,
    trailing?.current_sl,
    row?.sl_price,
    isStopExit ? row?.exit_price : null,
  );
  const currentSlIsFinite = Number.isFinite(currentSl);

  if (!activated) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-gray-500">
        <span className="h-1.5 w-1.5 rounded-full bg-gray-600" />
        {breakevenPolicy && Number.isFinite(Number(breakeven.activation_price))
          ? `arms at ${Number(breakeven.activation_price).toFixed(2)}`
          : 'OFF'}
      </span>
    );
  }

  // Delta relative to the initial SL. For BUY exits the trailed SL rises
  // (positive delta = protection tightened). For SELL exits it falls
  // (delta shown as negative movement in absolute terms).
  const delta = Number.isFinite(initialSl) && currentSlIsFinite
    ? (side === 'SELL' ? initialSl - currentSl! : currentSl! - initialSl)
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
  const bumps = Math.max(Number(trailing?.update_count) || 0, events.length);
  const legacySummaryOnly = activated && bumps > 0 && !events.length;

  return (
    <>
    <div className="text-xs text-gray-300">
      <div className="flex items-center gap-1 font-semibold text-[#22c55e]">
        <span className="h-1.5 w-1.5 rounded-full bg-[#22c55e]" />
        {breakevenPolicy ? 'BE' : arrow} {currentSlIsFinite ? currentSl!.toFixed(2) : '--'}
        <span className="text-gray-400">{deltaLabel}</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-gray-500">
        <span>{breakevenPolicy ? `breakeven armed${firstAt ? ` ${firstAt}` : ''}` : (firstAt ? `active ${firstAt}` : 'active')} · {bumps}x{Number.isFinite(initialSl) ? ` · init ${initialSl.toFixed(2)}` : ''}</span>
        <button
          type="button"
          onClick={() => setHistoryOpen(true)}
          className="inline-flex items-center rounded border border-[#1d4ed8] px-2 py-0.5 text-[10px] font-medium text-[#60a5fa] transition hover:bg-[#0f172a]"
        >
          Trail list
        </button>
      </div>
    </div>
    {historyOpen && (
      <div
        className="fixed inset-0 z-[90] bg-black/60 p-3 sm:p-6"
        onClick={() => setHistoryOpen(false)}
      >
        <div
          className="mx-auto w-full max-w-3xl rounded-xl border border-[#1f2937] bg-[#0b1220] shadow-2xl"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="flex items-start justify-between gap-4 border-b border-[#1f2937] px-4 py-3 sm:px-5">
            <div>
              <h4 className="text-lg font-semibold text-gray-100">{breakevenPolicy ? 'Breakeven stop history' : 'Trailing SL history'}</h4>
              <p className="mt-1 text-sm text-gray-400">
                {row?.symbol || 'Position'} · {side || '--'} · {bumps} saved trail {bumps === 1 ? 'move' : 'moves'}
              </p>
            </div>
            <button
              type="button"
              onClick={() => setHistoryOpen(false)}
              className="rounded p-1 text-gray-400 transition hover:bg-[#111827] hover:text-gray-200"
              aria-label="Close trailing SL history"
            >
              <i className="ri-close-line text-xl" />
            </button>
          </div>
          <div className="grid gap-3 border-b border-[#1f2937] px-4 py-3 text-sm sm:grid-cols-4 sm:px-5">
            <div>
              <div className="label text-[10px]">Activated</div>
              <div className="mt-1 font-medium text-gray-100">{firstAt || '--'}</div>
            </div>
            <div>
              <div className="label text-[10px]">Initial SL</div>
              <div className="mt-1 font-medium text-gray-100">{formatNumber(initialSl)}</div>
            </div>
            <div>
              <div className="label text-[10px]">Current SL</div>
              <div className="mt-1 font-medium text-gray-100">{formatNumber(currentSl)}</div>
            </div>
            <div>
              <div className="label text-[10px]">Protected</div>
              <div className="mt-1 font-medium text-[#22c55e]">{deltaLabel || '--'}</div>
            </div>
          </div>
          <div className="max-h-[60vh] overflow-auto px-4 py-3 sm:px-5">
            {!events.length ? (
              <div className="space-y-2 text-sm text-gray-400">
                <p>
                  {legacySummaryOnly
                    ? 'This older trade only saved the trailing summary. Activation time and total bumps are available, but the per-step trail list was not captured for that trade.'
                    : 'No trailing-stop step history was saved for this trade.'}
                </p>
                {legacySummaryOnly ? (
                  <p className="text-xs text-gray-500">
                    Newer trades save every trail move, so this modal will show the full list going forward.
                  </p>
                ) : null}
              </div>
            ) : (
              <div className="overflow-x-auto rounded border border-[#1f2937]">
                <table className="w-full min-w-[640px] border-collapse text-xs">
                  <thead className="bg-[#111827]">
                    <tr>
                      {['#', 'Time', 'LTP', 'Previous SL', 'New SL', 'Delta'].map((column) => (
                        <th key={column} className="table-cell label">{column}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {events.map((event: any, index: number) => {
                      const previous = Number(event?.previous_sl);
                      const next = Number(event?.new_sl);
                      const eventDelta = Number(event?.delta);
                      return (
                        <tr key={`${event?.at || 'trail'}-${index}`} className={index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
                          <td className="table-cell num text-gray-500">{index + 1}</td>
                          <td className="table-cell num text-gray-300">{formatDateTimeWithDate(event?.at)}</td>
                          <td className="table-cell num text-gray-100">{formatNumber(event?.ltp)}</td>
                          <td className="table-cell num text-gray-100">{formatNumber(previous)}</td>
                          <td className="table-cell num font-semibold text-[#22c55e]">{formatNumber(next)}</td>
                          <td className="table-cell num font-semibold text-[#22c55e]">
                            {Number.isFinite(eventDelta) ? `${eventDelta >= 0 ? '+' : ''}${formatNumber(eventDelta)}` : '--'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
    )}
    </>
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

function optionalPositiveNumber(...values: unknown[]) {
  for (const value of values) {
    if (value === null || value === undefined || value === '') continue;
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return number;
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
  const date = parseMarketTimestamp(value);
  if (!date) return '--';
  return date.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

// Same as formatDateTime but prefixes the date. Used for setup / last-bar
// labels where "set at 16:15" is ambiguous — could be today's 16:15 or
// yesterday's stale value carried through warmup. Rendered as
// "20 Aug 16:15:00" so the user always knows the day.
function formatDateTimeWithDate(value: unknown) {
  const date = parseMarketTimestamp(value);
  if (!date) return value ? String(value) : '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function formatDateOnly(value: unknown) {
  const date = parseMarketTimestamp(value);
  if (!date) return value ? String(value) : '--';
  return date.toLocaleDateString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

function formatTimeOnly(value: unknown) {
  const date = parseMarketTimestamp(value);
  if (!date) return value ? String(value) : '--';
  return date.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function toKolkataDateKey(value: unknown) {
  const date = parseMarketTimestamp(value);
  if (!date) return null;
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date);
  const year = parts.find((part) => part.type === 'year')?.value;
  const month = parts.find((part) => part.type === 'month')?.value;
  const day = parts.find((part) => part.type === 'day')?.value;
  if (!year || !month || !day) return null;
  return `${year}-${month}-${day}`;
}

function formatTradeTime(value: unknown, pairedValue: unknown) {
  const date = parseMarketTimestamp(value);
  if (!date) return '--';
  const pairedDate = parseMarketTimestamp(pairedValue);
  // A closed-trades row can legitimately span dates after an outage/manual
  // recovery. Show the date in that exceptional case so time-only cells do
  // not falsely look like an exit happened before its entry.
  if (pairedDate && toKolkataDateKey(value) !== toKolkataDateKey(pairedValue)) {
    return formatDateTimeWithDate(value);
  }
  return formatDateTime(value);
}

function parseMarketTimestamp(value: unknown): Date | null {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  // Strategy candle timestamps without an offset are IST by contract. Attach
  // the offset explicitly so browser locale/timezone never changes the shown
  // market time. Broker fills now include UTC offsets and bypass this branch.
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
  const normalized = hasOffset ? raw : `${raw.replace(' ', 'T')}+05:30`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function buildSetupHistoryGroups(rows: any[]) {
  const groups = new Map<string, { dateKey: string; dateLabel: string; latestTime: number; rows: any[] }>();
  for (const row of rows) {
    const dateKey = toKolkataDateKey(row?.candle_time);
    if (!dateKey) continue;
    const candleTime = new Date(String(row.candle_time)).getTime();
    const current = groups.get(dateKey);
    if (!current) {
      groups.set(dateKey, {
        dateKey,
        dateLabel: formatDateOnly(row.candle_time),
        latestTime: candleTime,
        rows: [row],
      });
      continue;
    }
    current.rows.push(row);
    if (candleTime > current.latestTime) current.latestTime = candleTime;
  }
  return Array.from(groups.values())
    .map((group) => ({
      ...group,
      // Setup history is a live audit view, so show the newest captured
      // reference first instead of making users scroll to the bottom.
      rows: [...group.rows].sort((a, b) => new Date(String(b.candle_time)).getTime() - new Date(String(a.candle_time)).getTime()),
    }))
    .sort((a, b) => b.latestTime - a.latestTime);
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
  const normalized = String(reason || '').toUpperCase();
  if (normalized === 'TARGET' || normalized === 'TARGET_FYERS') return 'text-[#22c55e]';
  if (normalized === 'TRAILING_SL' || normalized === 'TRAILING_SL_FYERS') return 'text-[#f59e0b]';
  if (normalized === 'SL' || normalized === 'SL_FYERS') return 'text-[#ef4444]';
  if (normalized === 'EOD_SQUAREOFF' || normalized === 'EOD') return 'text-[#f59e0b]';
  return 'text-gray-100';
}

function reasonIcon(reason: string) {
  const normalized = String(reason || '').toUpperCase();
  if (normalized === 'TARGET' || normalized === 'TARGET_FYERS') return <i className="ri-checkbox-circle-fill mr-1 text-sm text-[#22c55e]" />;
  if (normalized === 'TRAILING_SL' || normalized === 'TRAILING_SL_FYERS') return <i className="ri-route-fill mr-1 text-sm text-[#f59e0b]" />;
  if (normalized === 'SL' || normalized === 'SL_FYERS') return <i className="ri-close-circle-fill mr-1 text-sm text-[#ef4444]" />;
  if (normalized === 'EOD_SQUAREOFF' || normalized === 'EOD') return <i className="ri-error-warning-fill mr-1 text-sm text-[#f59e0b]" />;
  return null;
}

function formatReason(reason: string) {
  const normalized = String(reason || '').toUpperCase();
  if (normalized === 'EOD_SQUAREOFF') return 'EOD';
  if (normalized === 'TRAILING_SL' || normalized === 'TRAILING_SL_FYERS') return 'Trailing SL';
  if (normalized === 'SL_FYERS') return 'SL';
  return reason || '--';
}

function formatTrigger(trigger: unknown) {
  const value = String(trigger || '').trim();
  return value || 'Legacy row: trigger was not stored when this trade opened.';
}
