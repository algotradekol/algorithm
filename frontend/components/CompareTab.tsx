'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const POLL_MS = 5000;

export default function CompareTab() {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const result = await api.compare();
        if (!cancelled) {
          setData(result);
          setError('');
        }
      } catch (e: any) {
        if (!cancelled) setError(e?.message || 'Failed to load comparison');
        console.error(e);
      }
    }
    poll();
    const interval = setInterval(() => {
      if (!document.hidden) poll();
    }, POLL_MS);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  if (!data) return <p>Loading comparison...</p>;

  const algoIds = Object.keys(data);
  const rows: [string, string][] = [
    ['cash', 'Cash'],
    ['trade_count_today', 'Trades Today'],
    ['buy_count_today', 'Buy Trades'],
    ['sell_count_today', 'Sell Trades'],
    ['realized_gross_pnl', 'Gross P&L'],
    ['realized_charges', 'Total Charges'],
    ['realized_net_pnl', 'Net P&L'],
  ];

  return (
    <div>
      <h3>Compare - same inputs, same day</h3>
      {error && <p style={{ color: '#ff6b6b', marginBottom: 12 }}>{error}</p>}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
        <thead>
          <tr>
            <th style={{ textAlign: 'left', padding: 8, borderBottom: '1px solid #2a3441' }}></th>
            {algoIds.map((id) => (
              <th key={id} style={{ textAlign: 'left', padding: 8, borderBottom: '1px solid #2a3441' }}>{id}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([key, label]) => (
            <tr key={key}>
              <td style={{ padding: 8, borderBottom: '1px solid #1e2530', color: '#8a94a3' }}>{label}</td>
              {algoIds.map((id) => (
                <td key={id} style={{
                  padding: 8, borderBottom: '1px solid #1e2530',
                  color: key === 'realized_net_pnl' ? (data[id][key] >= 0 ? '#4ade80' : '#ff6b6b') : '#fff',
                  fontWeight: key === 'realized_net_pnl' ? 600 : 400,
                }}>
                  {typeof data[id][key] === 'number' && key.includes('pnl') || key === 'cash' || key === 'realized_charges'
                    ? `Rs ${data[id][key].toLocaleString()}`
                    : data[id][key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p style={{ color: '#8a94a3', fontSize: 12, marginTop: 16 }}>
        When you add a 3rd algo, it appears here automatically - this table just reads
        whatever algos the backend reports.
      </p>
    </div>
  );
}
