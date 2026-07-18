'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const POLL_MS = 5000;

export default function AlgoTab({
  algoId,
  displayName,
  description,
}: {
  algoId: string;
  displayName: string;
  description?: string;
}) {
  const [summary, setSummary] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<any[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const [summaryResult, positionsResult, tradesResult] = await Promise.allSettled([
        api.summary(algoId), api.positions(algoId), api.trades(algoId),
      ]);
      if (cancelled) return;

      if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value);
      if (positionsResult.status === 'fulfilled') setPositions(positionsResult.value);
      if (tradesResult.status === 'fulfilled') setTrades(tradesResult.value);

      const failures = [summaryResult, positionsResult, tradesResult]
        .filter((result) => result.status === 'rejected')
        .map((result) => (result as PromiseRejectedResult).reason?.message || 'Request failed');

      setError(failures[0] || '');
    }
    poll();
    const interval = setInterval(() => {
      if (!document.hidden) poll();
    }, POLL_MS);
    return () => { cancelled = true; clearInterval(interval); };
  }, [algoId]);

  if (!summary) {
    return (
      <section className="panel p-4">
        <h2 className="text-base font-semibold text-gray-100">{displayName}</h2>
        <p className="mt-2 text-sm text-gray-500">{error || 'Loading strategy data...'}</p>
      </section>
    );
  }

  const startingCapital = Number(summary.starting_capital || 0);
  const cash = Number(summary.cash || 0);
  const netPnl = Number(summary.realized_net_pnl || 0);
  const grossPnl = Number(summary.realized_gross_pnl || 0);
  const equityDelta = cash - startingCapital;

  return (
    <section className="space-y-4">
      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}

      <div className="grid grid-cols-3 gap-2 lg:grid-cols-6">
        <MetricCard label="Cash Remaining" value={formatMoney(cash)} />
        <MetricCard label="Equity" value={formatMoney(cash)} delta={formatSignedMoney(equityDelta)} pnl={equityDelta} />
        <MetricCard label="Trades Today" value={`${summary.trade_count_today} / 10`} />
        <MetricCard label="Buy / Sell" value={`${summary.buy_count_today}B ${summary.sell_count_today}S`} />
        <MetricCard label="Gross P&L" value={formatMoney(grossPnl)} pnl={grossPnl} />
        <MetricCard label="Net P&L" value={formatMoney(netPnl)} pnl={netPnl} important />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">Open Positions</h3>
          <PositionsTable rows={positions} />
        </section>

        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">Today's Trades</h3>
          <TradesTable rows={trades} />
        </section>
      </div>

      {description && (
        <div className="rounded border border-[#1f2937] bg-[#111827] px-3 py-2 text-xs text-gray-500">
          {description}
        </div>
      )}
    </section>
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
    <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="label">{label}</div>
      <div className={`num mt-2 font-semibold ${important ? 'text-2xl' : 'text-base'} ${pnlColor(pnl)}`}>
        {value}
      </div>
      {delta && <div className={`num mt-1 text-xs ${pnlColor(pnl)}`}>{delta} vs start</div>}
    </div>
  );
}

function PositionsTable({ rows }: { rows: any[] }) {
  return (
    <div className="overflow-x-auto rounded border border-[#1f2937]">
      <table className="w-full min-w-max border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {['Symbol', 'Side', 'Qty', 'Entry', 'LTP', 'SL', 'Target', 'Unreal P&L'].map((column) => (
              <th key={column} className="table-cell label">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={8} className="table-cell text-gray-500">No open positions</td>
            </tr>
          ) : rows.map((row, index) => {
            const ltp = Number(row.ltp ?? row.last_ltp ?? row._last_ltp);
            const entry = Number(row.entry_price || 0);
            const qty = Number(row.qty || 0);
            const unreal = Number.isFinite(ltp)
              ? (row.side === 'SELL' ? entry - ltp : ltp - entry) * qty
              : null;
            return (
              <tr key={row.id || index} className={index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
                <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
                <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                  {row.side === 'SELL' ? 'S' : 'B'}
                </td>
                <td className="table-cell num text-gray-100">{row.qty}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.entry_price)}</td>
                <td className="table-cell num text-gray-100">{Number.isFinite(ltp) ? formatNumber(ltp) : '--'}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.sl_price)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.target_price)}</td>
                <td className={`table-cell num font-semibold ${pnlColor(unreal)}`}>{unreal === null ? '--' : formatMoney(unreal)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TradesTable({ rows }: { rows: any[] }) {
  return (
    <div className="overflow-x-auto rounded border border-[#1f2937]">
      <table className="w-full min-w-max border-collapse text-xs">
        <thead className="bg-[#111827]">
          <tr>
            {['Symbol', 'Side', 'Entry', 'Exit', 'Reason', 'Gross', 'Charges', 'Net'].map((column) => (
              <th key={column} className="table-cell label">{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length ? (
            <tr className="bg-[#0d1117]">
              <td colSpan={8} className="table-cell text-gray-500">No trades today</td>
            </tr>
          ) : rows.map((row, index) => (
            <tr key={row.id || index} className={index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
              <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
              <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                {row.side === 'SELL' ? 'S' : 'B'}
              </td>
              <td className="table-cell num text-gray-100">{formatNumber(row.entry_price)}</td>
              <td className="table-cell num text-gray-100">{formatNumber(row.exit_price)}</td>
              <td className={`table-cell font-semibold ${reasonColor(row.exit_reason)}`}>{formatReason(row.exit_reason)}</td>
              <td className={`table-cell num ${pnlColor(Number(row.gross_pnl || 0))}`}>{formatMoney(row.gross_pnl)}</td>
              <td className="table-cell num text-gray-100">{formatMoney(row.total_charges)}</td>
              <td className={`table-cell num font-semibold ${pnlColor(Number(row.net_pnl || 0))}`}>{formatMoney(row.net_pnl)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Table({ rows, columns }: { rows: any[]; columns: string[] }) {
  if (!rows.length) return <p className="text-sm text-gray-500">Nothing here yet.</p>;
  return (
    <div className="mb-6 overflow-x-auto rounded border border-[#1f2937]">
      <table className="w-full min-w-max border-collapse text-sm">
        <thead className="bg-[#111827]">
          <tr>{columns.map((c) => (
            <th key={c} className="table-cell label">{c}</th>
          ))}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className={i % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
              {columns.map((c) => (
                <td key={c} className="table-cell text-gray-100">{String(row[c] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatMoney(value: unknown) {
  const number = Number(value || 0);
  return `Rs ${number.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
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

function formatReason(reason: string) {
  if (reason === 'EOD_SQUAREOFF') return 'EOD';
  return reason || '--';
}
