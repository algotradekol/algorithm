'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const POLL_MS = 5000;

export default function AlgoTab({ algoId, displayName }: { algoId: string; displayName: string }) {
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
      failures.forEach((message) => console.error(message));
    }
    poll();
    const interval = setInterval(() => {
      if (!document.hidden) poll();
    }, POLL_MS);
    return () => { cancelled = true; clearInterval(interval); };
  }, [algoId]);

  if (!summary) return <p>Loading {displayName}...</p>;

  return (
    <section>
      <h2 className="text-xl font-semibold text-white">{displayName}</h2>
      {error && <p className="mt-3 text-sm text-danger">{error}</p>}
      <div className="my-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card label="Cash" value={`Rs ${summary.cash.toLocaleString()}`} />
        <Card label="Trades Today" value={`${summary.trade_count_today} (B:${summary.buy_count_today} S:${summary.sell_count_today})`} />
        <Card label="Gross P&L Today" value={`Rs ${summary.realized_gross_pnl.toLocaleString()}`} />
        <Card label="Net P&L Today" value={`Rs ${summary.realized_net_pnl.toLocaleString()}`} highlight />
      </div>

      <h3 className="mb-3 mt-6 text-sm font-semibold uppercase tracking-[0.14em] text-textSoft">Open Positions</h3>
      <Table
        rows={positions}
        columns={['symbol', 'side', 'qty', 'entry_price', 'sl_price', 'target_price']}
      />

      <h3 className="mb-3 mt-6 text-sm font-semibold uppercase tracking-[0.14em] text-textSoft">Trade Log (charges + net P&amp;L per trade)</h3>
      <Table
        rows={trades}
        columns={['symbol', 'side', 'qty', 'entry_price', 'exit_price', 'exit_reason',
          'gross_pnl', 'total_charges', 'net_pnl']}
      />
    </section>
  );
}

function Card({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className="panel p-4">
      <div className="text-xs text-textSoft">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${highlight ? 'text-success' : 'text-white'}`}>{value}</div>
    </div>
  );
}

export function Table({ rows, columns }: { rows: any[]; columns: string[] }) {
  if (!rows.length) return <p className="text-sm text-textSoft">Nothing here yet.</p>;
  return (
    <div className="mb-6 overflow-x-auto rounded-lg border border-line">
      <table className="w-full min-w-max border-collapse text-sm">
        <thead className="bg-panelSoft">
          <tr>{columns.map((c) => (
            <th key={c} className="table-cell text-xs font-medium uppercase tracking-[0.1em] text-textSoft">{c}</th>
          ))}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="odd:bg-panel/60 even:bg-ink/50">
              {columns.map((c) => (
                <td key={c} className="table-cell text-white">{String(row[c] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
