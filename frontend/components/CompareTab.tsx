'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const POLL_MS = 30_000;

export default function CompareTab() {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState('');
  const [lastUpdated, setLastUpdated] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const result = await api.compare();
        if (!cancelled) {
          setData(result);
          setLastUpdated(new Date().toLocaleTimeString('en-IN', { hour12: false }));
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

  if (!data) return <p className="text-sm text-gray-500">Loading comparison...</p>;

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
    <section className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-base font-semibold text-gray-100">Compare - same inputs, same day</h2>
        <div className="text-xs uppercase tracking-wider text-gray-500">
          Last updated <span className="num text-gray-300">{lastUpdated || '--'}</span>
        </div>
      </div>
      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}
      <div className="overflow-x-auto rounded border border-[#1f2937]">
        <table className="w-full min-w-max border-collapse text-xs">
          <thead className="bg-[#111827]">
            <tr>
              <th className="table-cell label sticky left-0 z-20 min-w-32 bg-[#111827]">Metric</th>
              {algoIds.map((id) => (
                <th key={id} className="table-cell label">{id}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, label], rowIndex) => {
              const rowBg = rowIndex % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]';
              return (
              <tr key={key} className={rowBg}>
                <td className={`table-cell sticky left-0 z-10 min-w-32 text-gray-500 ${rowBg}`}>{label}</td>
                {algoIds.map((id) => {
                  const value = data[id][key];
                  const isMoney = key.includes('pnl') || key === 'cash' || key === 'realized_charges';
                  const isNet = key === 'realized_net_pnl';
                  const pnl = Number(value || 0);
                  return (
                    <td
                      key={id}
                      className={`table-cell num ${isNet ? 'text-2xl font-semibold' : 'text-sm'} ${
                        key.includes('pnl') ? pnlColor(pnl) : 'text-gray-100'
                      }`}
                    >
                      {key.includes('pnl') && pnl > 0 && <i className="ri-arrow-up-circle-fill mr-1 text-sm text-[#22c55e]" />}
                      {key.includes('pnl') && pnl < 0 && <i className="ri-arrow-down-circle-fill mr-1 text-sm text-[#ef4444]" />}
                      {isMoney ? formatMoney(value) : value}
                    </td>
                  );
                })}
              </tr>
            );
            })}
          </tbody>
        </table>
      </div>
      <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
        <div className="space-y-1 text-xs text-gray-500">
          <p>Algo 1: Opening Range Gap</p>
          <p>Algo 2: VWAP/EMA/Volume Momentum</p>
          <p>Algo 3: Opening Range Gap (Basic) - pure price action, no indicators</p>
          <p>Algo 4: Opening Range Gap (With Indicators) - price action + momentum confirmation filters</p>
          <p>Algo 5: Afternoon Candle Continuation - scheduled 2:00 PM candle signal, 2:02 PM entry test</p>
        </div>
      </div>
    </section>
  );
}

function formatMoney(value: unknown) {
  const number = Number(value || 0);
  return `Rs ${number.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function pnlColor(value: number) {
  if (value > 0) return 'text-[#22c55e]';
  if (value < 0) return 'text-[#ef4444]';
  return 'text-gray-100';
}
