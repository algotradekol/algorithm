'use client';
import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import StrategySettingsPanel from './StrategySettingsPanel';
import ScanResultsPanel from './ScanResultsPanel';
import { useWebSocket, WebSocketState } from '../lib/useWebSocket';
import { PAGE_SIZE, PaginationControls } from './PaginationControls';

const FALLBACK_POLL_MS = 30_000;

export default function AlgoTab({
  algoId,
  displayName,
  description,
  onWebSocketStatus,
}: {
  algoId: string;
  displayName: string;
  description?: string;
  onWebSocketStatus?: (status: WebSocketState) => void;
}) {
  const [summary, setSummary] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<any[]>([]);
  const [scanResults, setScanResults] = useState<any>(null);
  const [error, setError] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);

  const loadData = useCallback(async () => {
    const [summaryResult, positionsResult, tradesResult, scanResult] = await Promise.allSettled([
      api.summary(algoId), api.positions(algoId), api.trades(algoId), api.scanResults(algoId),
    ]);

    if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value);
    if (positionsResult.status === 'fulfilled') {
      setPositions(positionsResult.value.map((position: any) => ({
        ...position,
        ltp: position.ltp ?? position.last_ltp ?? position._last_ltp ?? position.entry_price,
        unrealized_pnl: position.unrealized_pnl ?? 0,
      })));
    }
    if (tradesResult.status === 'fulfilled') setTrades(tradesResult.value);
    if (scanResult.status === 'fulfilled') setScanResults(scanResult.value);

    const failures = [summaryResult, positionsResult, tradesResult]
      .filter((result) => result.status === 'rejected')
      .map((result) => (result as PromiseRejectedResult).reason?.message || 'Request failed');
    setError(failures[0] || '');
  }, [algoId]);

  useEffect(() => {
    let cancelled = false;
    loadData();
    const interval = setInterval(() => {
      if (!document.hidden && !cancelled) loadData();
    }, FALLBACK_POLL_MS);
    return () => { cancelled = true; clearInterval(interval); };
  }, [loadData]);

  const handleWsMessage = useCallback((message: any) => {
    if (message.event === 'price_update') {
      setPositions((current) => current.map((position) => (
        position.symbol === message.symbol ? {
          ...position,
          ltp: message.ltp,
          high_price: Math.max(Number(position.high_price ?? position.highest_price ?? position.entry_price ?? message.ltp), Number(message.ltp)),
          low_price: Math.min(Number(position.low_price ?? position.lowest_price ?? position.entry_price ?? message.ltp), Number(message.ltp)),
          unrealized_pnl: calculateUnrealized(position, message.ltp),
        } : position
      )));
      return;
    }

    if (message.algo_id !== algoId) return;

    if (message.event === 'position_opened') {
      setPositions((current) => [{ ...message, ltp: message.ltp ?? message.entry_price, status: 'open' }, ...current]);
    } else if (message.event === 'position_closed') {
      setPositions((current) => current.filter((position) => position.symbol !== message.symbol));
      setTrades((current) => [message, ...current]);
      api.summary(algoId).then(setSummary).catch(() => {});
    } else if (message.event === 'scan_complete') {
      setScanResults(message.results);
    }
  }, [algoId]);

  useWebSocket(handleWsMessage, true, onWebSocketStatus);

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
        <SettingsDrawer open={settingsOpen} algoId={algoId} onClose={() => setSettingsOpen(false)} />
        <div className="mt-4">
          <ScanResultsPanel results={scanResults} />
        </div>
        <p className="mt-2 text-sm text-gray-500">{error || 'Loading strategy data...'}</p>
      </section>
    );
  }

  const startingCapital = Number(summary.starting_capital || 0);
  const cash = Number(summary.cash || 0);
  const netPnl = Number(summary.realized_net_pnl || 0);
  const grossPnl = Number(summary.realized_gross_pnl || 0);
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

      <div className="grid grid-cols-3 gap-1.5 sm:gap-2 lg:grid-cols-6">
        <MetricCard label="Total Capital" value={formatMoney(totalCapital)} delta={formatSignedMoney(totalCapital - startingCapital)} pnl={totalCapital - startingCapital} />
        <MetricCard label="Capital Used" value={formatMoney(capitalUsed)} />
        <MetricCard label="Trades Today" value={`${summary.trade_count_today} / ${summary.max_trades_per_day || 10}`} />
        <MetricCard label="Buy / Sell" value={`${summary.buy_count_today}B ${summary.sell_count_today}S`} />
        <MetricCard label="Realized Gross P&L" value={formatMoney(grossPnl)} pnl={grossPnl} />
        <MetricCard label="Live Net P&L" value={formatMoney(liveNetPnl)} pnl={liveNetPnl} important />
      </div>

      <SettingsDrawer open={settingsOpen} algoId={algoId} onClose={() => setSettingsOpen(false)} />

      <ScanResultsPanel results={scanResults} />

      <div className="grid gap-4">
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">Open Positions</h3>
          <PositionsTable rows={positions} />
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

function SettingsDrawer({ open, algoId, onClose }: { open: boolean; algoId: string; onClose: () => void }) {
  return (
    <div className={`overflow-hidden transition-opacity duration-300 ${open ? 'opacity-100' : 'max-h-0 opacity-0'}`}>
      <div className="mt-4 rounded border border-[#1f2937] bg-[#0d1117] p-3">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="label">Strategy Settings</div>
          <button onClick={onClose} className="text-sm text-gray-500 hover:text-gray-100">X</button>
        </div>
        <StrategySettingsPanel algoId={algoId} />
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
}: {
  label: string;
  value: string;
  delta?: string;
  pnl?: number;
  important?: boolean;
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
    </div>
  );
}

function PositionsTable({ rows }: { rows: any[] }) {
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
                <div className="font-mono text-sm text-gray-100">{row.symbol}</div>
                <div className={`num flex items-center gap-1 text-base font-semibold ${pnlColor(unreal)}`}>{unreal === null ? '--' : formatMoney(unreal)}</div>
              </div>
              <div className={`mt-1 inline-flex items-center gap-1 text-sm font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                <i className={`${row.side === 'SELL' ? 'ri-indeterminate-circle-fill' : 'ri-add-circle-fill'} text-sm`} />
                {row.side === 'SELL' ? 'S' : 'B'}
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-gray-500">
                <MobileField label="Qty" value={row.qty} />
                <MobileField label="Entry" value={formatNumber(row.entry_price)} />
                <MobileField label="LTP" value={Number.isFinite(ltp) ? formatNumber(ltp) : '--'} />
                <MobileField label="High" value={formatNumber(row.high_price ?? row.highest_price)} />
                <MobileField label="Low" value={formatNumber(row.low_price ?? row.lowest_price)} />
                <MobileField label="SL" value={formatNumber(row.sl_price)} />
                <MobileField label="Target" value={formatNumber(row.target_price)} />
                <MobileField label="Trigger" value={formatTrigger(row.entry_trigger)} wide />
              </div>
            </div>
          );
        })}
      </div>
      <div className="hidden overflow-x-auto rounded border border-[#1f2937] sm:block">
        <table className="w-full min-w-[1040px] border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {['Symbol', 'Side', 'Qty', 'Entry', 'LTP', 'High', 'Low', 'SL', 'Target', 'Trigger', 'Unreal P&L'].map((column) => (
              <th key={column} className="table-cell label">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={11} className="table-cell text-gray-500">No open positions</td>
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
                <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
                <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                  <i className={`${row.side === 'SELL' ? 'ri-indeterminate-circle-fill' : 'ri-add-circle-fill'} mr-1 text-sm`} />
                  {row.side === 'SELL' ? 'S' : 'B'}
                </td>
                <td className="table-cell num text-gray-100">{row.qty}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.entry_price)}</td>
                <td className="table-cell num text-gray-100">{Number.isFinite(ltp) ? formatNumber(ltp) : '--'}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.high_price ?? row.highest_price)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.low_price ?? row.lowest_price)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.sl_price)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.target_price)}</td>
                <td className="table-cell max-w-[300px] text-gray-400">{formatTrigger(row.entry_trigger)}</td>
                <td className={`table-cell num font-semibold ${pnlColor(unreal)}`}>{unreal === null ? '--' : formatMoney(unreal)}</td>
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
        {!rows.length ? <p className="rounded border border-[#1f2937] bg-[#0d1117] p-3 text-sm text-gray-500">No closed trades yet</p> : visibleRows.map((row, index) => (
          <div key={row.id || index} className={`rounded border border-[#1f2937] p-3 ${index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}`}>
            <div className="flex items-center justify-between gap-3">
              <div className="font-mono text-sm text-gray-100">{row.symbol}</div>
              <div className={`num flex items-center gap-1 text-base font-semibold ${pnlColor(Number(row.net_pnl || 0))}`}>
                {Number(row.net_pnl || 0) > 0 && <i className="ri-arrow-up-circle-fill text-sm text-[#22c55e]" />}
                {Number(row.net_pnl || 0) < 0 && <i className="ri-arrow-down-circle-fill text-sm text-[#ef4444]" />}
                {formatMoney(row.net_pnl)}
              </div>
            </div>
            <div className={`mt-1 inline-flex items-center gap-1 text-sm font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
              <i className={`${row.side === 'SELL' ? 'ri-indeterminate-circle-fill' : 'ri-add-circle-fill'} text-sm`} />
              {row.side === 'SELL' ? 'S' : 'B'}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-gray-500">
              <MobileField label="Entry" value={formatNumber(row.entry_price)} />
              <MobileField label="Exit" value={formatNumber(row.exit_price)} />
              <MobileField label="Reason" value={formatReason(row.exit_reason)} />
              <MobileField label="Trigger" value={formatTrigger(row.entry_trigger)} wide />
              <MobileField label="Gross" value={formatMoney(row.gross_pnl)} />
              <MobileField label="Charges" value={formatMoney(row.total_charges)} />
            </div>
          </div>
        ))}
      </div>
      <div className="hidden overflow-x-auto rounded border border-[#1f2937] sm:block">
        <table className="w-full min-w-[1080px] border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {['Symbol', 'Side', 'Entry', 'Exit', 'Reason', 'Trigger', 'Gross', 'Charges', 'Net'].map((column) => (
              <th key={column} className="table-cell label">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={9} className="table-cell text-gray-500">No closed trades yet</td>
            </tr>
          ) : visibleRows.map((row, index) => (
            <tr key={row.id || index} className={index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
              <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
              <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                <i className={`${row.side === 'SELL' ? 'ri-indeterminate-circle-fill' : 'ri-add-circle-fill'} mr-1 text-sm`} />
                {row.side === 'SELL' ? 'S' : 'B'}
              </td>
              <td className="table-cell num text-gray-100">{formatNumber(row.entry_price)}</td>
              <td className="table-cell num text-gray-100">{formatNumber(row.exit_price)}</td>
              <td className={`table-cell font-semibold ${reasonColor(row.exit_reason)}`}>
                {reasonIcon(row.exit_reason)}
                {formatReason(row.exit_reason)}
              </td>
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

function MobileField({ label, value, wide = false }: { label: string; value: any; wide?: boolean }) {
  return (
    <div className={wide ? 'col-span-2' : ''}>
      <div className="label text-[10px]">{label}</div>
      <div className="num mt-0.5 text-gray-100">{value}</div>
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
