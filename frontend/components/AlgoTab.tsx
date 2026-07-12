'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

export default function AlgoTab({ algoId, displayName }: { algoId: string; displayName: string }) {
  const [summary, setSummary] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<any[]>([]);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const [s, p, t] = await Promise.all([
          api.summary(algoId), api.positions(algoId), api.trades(algoId),
        ]);
        if (!cancelled) { setSummary(s); setPositions(p); setTrades(t); }
      } catch (e) { console.error(e); }
    }
    poll();
    const interval = setInterval(poll, 3000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [algoId]);

  if (!summary) return <p>Loading {displayName}...</p>;

  return (
    <div>
      <h3>{displayName}</h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, marginBottom: 20 }}>
        <Card label="Cash" value={`₹${summary.cash.toLocaleString()}`} />
        <Card label="Trades Today" value={`${summary.trade_count_today} (B:${summary.buy_count_today} S:${summary.sell_count_today})`} />
        <Card label="Gross P&L Today" value={`₹${summary.realized_gross_pnl.toLocaleString()}`} />
        <Card label="Net P&L Today" value={`₹${summary.realized_net_pnl.toLocaleString()}`} highlight />
      </div>

      <h4>Open Positions</h4>
      <Table
        rows={positions}
        columns={['symbol', 'side', 'qty', 'entry_price', 'sl_price', 'target_price']}
      />

      <h4>Trade Log (charges + net P&L per trade)</h4>
      <Table
        rows={trades}
        columns={['symbol', 'side', 'qty', 'entry_price', 'exit_price', 'exit_reason',
          'gross_pnl', 'total_charges', 'net_pnl']}
      />
    </div>
  );
}

function Card({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div style={{ background: '#151b23', borderRadius: 10, padding: 14 }}>
      <div style={{ fontSize: 12, color: '#8a94a3' }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600, color: highlight ? '#4ade80' : '#fff' }}>{value}</div>
    </div>
  );
}

export function Table({ rows, columns }: { rows: any[]; columns: string[] }) {
  if (!rows.length) return <p style={{ color: '#8a94a3', fontSize: 13 }}>Nothing here yet.</p>;
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, marginBottom: 24 }}>
      <thead>
        <tr>{columns.map((c) => (
          <th key={c} style={{ textAlign: 'left', padding: 6, borderBottom: '1px solid #2a3441', color: '#8a94a3' }}>{c}</th>
        ))}</tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={i}>
            {columns.map((c) => (
              <td key={c} style={{ padding: 6, borderBottom: '1px solid #1e2530' }}>{String(row[c] ?? '')}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
