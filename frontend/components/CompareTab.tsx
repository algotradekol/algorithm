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
    <section>
      <h2 className="text-xl font-semibold text-white">Compare - same inputs, same day</h2>
      {error && <p className="mt-3 text-sm text-danger">{error}</p>}
      <div className="mt-5 overflow-x-auto rounded-lg border border-line">
        <table className="w-full min-w-max border-collapse text-sm">
          <thead className="bg-panelSoft">
            <tr>
              <th className="table-cell"></th>
              {algoIds.map((id) => (
                <th key={id} className="table-cell text-xs font-medium uppercase tracking-[0.1em] text-textSoft">{id}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, label]) => (
              <tr key={key} className="odd:bg-panel/60 even:bg-ink/50">
                <td className="table-cell text-textSoft">{label}</td>
                {algoIds.map((id) => (
                  <td
                    key={id}
                    className={`table-cell ${key === 'realized_net_pnl' ? 'font-semibold' : ''} ${
                      key === 'realized_net_pnl' ? (data[id][key] >= 0 ? 'text-success' : 'text-danger') : 'text-white'
                    }`}
                  >
                    {typeof data[id][key] === 'number' && key.includes('pnl') || key === 'cash' || key === 'realized_charges'
                      ? `Rs ${data[id][key].toLocaleString()}`
                      : data[id][key]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-4 text-xs text-textSoft">
        When you add a 3rd algo, it appears here automatically - this table just reads
        whatever algos the backend reports.
      </p>
    </section>
  );
}
