'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';

type Row = {
  id: string; symbol: string; side: string; lots?: number; entry_price?: number;
  limit_price?: number; stop_price?: number; margin?: number; margin_inr?: number;
  pnl_inr?: number; currency?: string; state: string; order_type: string; time?: string;
};
type Page = { rows: Row[]; next_cursor?: string; has_more?: boolean; fetched_at: number; note?: string; inr_rate?: number };
const views = { positions: 'Positions', open_orders: 'Open orders', stop_orders: 'Stop orders', history: 'Order history' };
const control = 'rounded border border-[#334155] bg-[#111827] px-3 py-2 text-sm text-gray-200 disabled:opacity-40';
const num = (value?: number) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: 4 });
const date = (value?: string | number) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '--';

export default function DeltaActivityTab({ enabledTimeframes }: { enabledTimeframes: number[] }) {
  const [source, setSource] = useState(enabledTimeframes.length ? 'paper' : 'live');
  const [minutes, setMinutes] = useState(enabledTimeframes[0] || 15);
  const [kind, setKind] = useState<keyof typeof views>('positions');
  // Remount on source/view changes so stale account rows never appear as paper.
  return <section className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h1 className="text-lg font-semibold text-gray-100">Delta activity</h1><p className="mt-1 text-sm text-gray-400">Paper simulation and actual Delta account records stay separate.</p></div>
      <div className="flex flex-wrap gap-2">
        <label className="text-xs text-gray-400">Data source<select aria-label="Activity data source" className={`${control} ml-2`} value={source} onChange={e => setSource(e.target.value)}>{enabledTimeframes.length > 0 && <option value="paper">Paper</option>}<option value="live">Live account (read-only)</option></select></label>
        {source === 'paper' && <select aria-label="Paper timeframe" className={control} value={minutes} onChange={e => setMinutes(Number(e.target.value))}>{enabledTimeframes.map(value => <option key={value} value={value}>Gold {value === 60 ? '1 hr' : value === 240 ? '4 hr' : `${value} min`}</option>)}</select>}
      </div>
    </div>
    <p className={`rounded border p-3 text-sm ${source === 'paper' ? 'border-[#3b82f6]/40 text-[#93c5fd]' : 'border-[#f59e0b]/40 text-[#fbbf24]'}`}>
      {source === 'paper' ? 'PAPER: stops and targets are virtual engine protection, not exchange orders.' : 'LIVE ACCOUNT / READ ONLY: all products on the configured Delta account. This does not enable live strategies or permit placing, editing or cancelling orders.'}
    </p>
    <nav className="flex flex-wrap gap-2" aria-label="Delta activity views">{Object.entries(views).map(([key, label]) => <button key={key} aria-pressed={kind === key} className={`${control} ${kind === key ? 'border-[#60a5fa] text-[#93c5fd]' : ''}`} onClick={() => setKind(key as keyof typeof views)}>{label}</button>)}</nav>
    <ActivityRows key={`${source}-${minutes}-${kind}`} source={source} minutes={minutes} kind={kind} />
  </section>;
}

function ActivityRows({ source, minutes, kind }: { source: string; minutes: number; kind: keyof typeof views }) {
  const [page, setPage] = useState<Page | null>(null);
  const [cursors, setCursors] = useState<string[]>(['']);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      setBusy(true);
      try {
        const result = await api.deltaAccount(kind, source, minutes, (cursors.length - 1) * 100, cursors[cursors.length - 1]);
        if (!cancelled) { setPage(result); setError(''); }
      } catch (err) {
        if (!cancelled) { setError(err instanceof Error ? err.message : 'Account data unavailable'); setPage(null); }
      } finally {
        if (!cancelled) { setBusy(false); timer = setTimeout(poll, 15000); }
      }
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [source, minutes, kind, cursors, refresh]);
  const headers = ['Symbol', 'Side', 'Lots', 'Type / reason', 'Status', 'Time (IST)', 'Entry / fill', 'Limit', 'Stop / target', source === 'paper' ? 'Est. entry margin' : 'Reported margin', 'Margin (INR)', 'Realized net (INR)'];
  return <>
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-400"><span>{page ? `Updated ${date(page.fetched_at)} IST` : busy ? 'Loading...' : 'Data unavailable'}{page?.inr_rate ? ` | Delta fixed conversion: 1 USD = Rs ${page.inr_rate}` : ''}</span><button className={control} disabled={busy} onClick={() => setRefresh(v => v + 1)}>Refresh</button></div>
    {error && <p role="alert" className="rounded border border-[#ef4444]/40 p-3 text-sm text-[#f87171]">{error}</p>}
    {page?.note && <p className="text-xs text-gray-400">{page.note}</p>}
    <p className="text-xs text-gray-500">1 displayed lot = 1 exchange quantity unit. Missing margin or P&amp;L is --, not zero. Paper margin excludes leverage overrides, size tiers and fees; live margin is shown only when Delta supplies it. {source === 'live' ? 'Order history is not a realized P&L ledger.' : ''}</p>
    <div className="max-h-[36rem] overflow-auto rounded border border-[#1f2937]"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="sticky top-0 bg-[#111827]"><tr>{headers.map(h => <th key={h} className="p-3 text-gray-400">{h}</th>)}</tr></thead><tbody>
      {page && !page.rows.length && <tr><td colSpan={headers.length} className="p-5 text-gray-500">No matching records on this page.{page.next_cursor ? ' More pages are available.' : ''}</td></tr>}
      {page?.rows.map((row, index) => <tr key={`${row.id}-${index}`} className="border-t border-[#1f2937] text-gray-200"><td className="p-3">{row.symbol}</td><td className={`p-3 ${row.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{row.side}</td>{[num(row.lots), row.order_type, row.state, date(row.time), num(row.entry_price), num(row.limit_price), num(row.stop_price), `${num(row.margin)} ${row.currency || ''}`, num(row.margin_inr), num(row.pnl_inr)].map((v, i) => <td key={i} className="p-3 font-mono">{v}</td>)}</tr>)}
    </tbody></table></div>
    <div className="flex justify-end gap-2"><button className={control} disabled={busy || cursors.length === 1} onClick={() => { setPage(null); setCursors(v => v.slice(0, -1)); }}>Previous</button><button className={control} disabled={busy || !(page?.next_cursor || page?.has_more)} onClick={() => { const cursor = page?.next_cursor || ''; setPage(null); setCursors(v => [...v, cursor]); }}>Next</button></div>
  </>;
}
