'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';

type Period = { trades: number; wins: number; losses: number; breakeven: number; win_rate: number; gross: number; fees: number; net: number };
type Outcome = {
  minutes: number; label: string; scan_enabled: boolean; trading_enabled: boolean; stale: boolean;
  last_bar_at?: number; ema20?: number; volume_ema20?: number; unrealized: number;
  combined_today: number; combined_all_time: number; today: Period; all_time: Period;
  cooldown?: { active: boolean; until?: number; remaining_seconds: number; reason?: string };
  position?: { side: string; entry_price: number; pnl_inr?: number };
};
type Overview = {
  generated_at: number; utc_date: string; ltp?: number; currency?: string; inr_rate?: number;
  timeframes: Outcome[];
  totals: { today: Period; all_time: Period; unrealized: number; today_with_unrealized: number; all_time_with_unrealized: number };
};

const number = (value?: number, digits = 2) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: digits });
const tone = (value?: number) => (value || 0) > 0 ? 'text-[#22c55e]' : (value || 0) < 0 ? 'text-[#ef4444]' : 'text-gray-300';
const time = (value?: number) => value ? new Date(value * 1000).toLocaleString('en-IN', { timeZone: 'UTC', hour12: false }) : '--';
const rest = (seconds?: number) => `${Math.floor((seconds || 0) / 60)}m ${Math.ceil((seconds || 0) % 60)}s`;

export default function DeltaOverviewTab() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await api.deltaOverview();
        if (!cancelled) { setData(result); setError(''); }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Delta overview unavailable');
      } finally {
        if (!cancelled) timer = setTimeout(poll, 3000);
      }
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);
  const currency = data?.currency || 'USD';
  const inr = (value?: number) => data?.inr_rate && value != null ? `Rs ${number(value * data.inr_rate)}` : '--';
  return <section className="space-y-4">
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div><h1 className="text-xl font-semibold text-gray-100">Delta strategy overview</h1><p className="mt-1 text-sm text-gray-400">All enabled PAXG paper strategies in one view. Daily totals reset at 00:00 UTC.</p></div>
      <div className="text-xs text-gray-500">Updated {data ? time(data.generated_at) : '--'} UTC</div>
    </header>
    {error && <p role="alert" className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 p-3 text-sm text-[#f87171]">{error}</p>}
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Summary label={`Today realized (${currency})`} value={data?.totals.today.net} inr={inr(data?.totals.today.net)} detail={`${number(data?.totals.today.trades, 0)} closed | ${number(data?.totals.today.win_rate)}% wins`} />
      <Summary label={`Open P&L (${currency})`} value={data?.totals.unrealized} inr={inr(data?.totals.unrealized)} detail="Across currently open simulations" />
      <Summary label={`Today + open (${currency})`} value={data?.totals.today_with_unrealized} inr={inr(data?.totals.today_with_unrealized)} detail="Today realized net plus open P&L" />
      <Summary label={`All-time + open (${currency})`} value={data?.totals.all_time_with_unrealized} inr={inr(data?.totals.all_time_with_unrealized)} detail={`${number(data?.totals.all_time.trades, 0)} all-time closed trades`} />
    </div>
    <div className="overflow-x-auto rounded border border-[#1f2937] bg-[#0d131e]">
      <table className="w-full min-w-[1320px] text-left text-xs">
        <thead className="bg-[#111827] text-gray-400"><tr>{['Timeframe', 'Controls', 'Entry rest', 'Feed / last bar UTC', 'EMA20', 'Volume EMA20', 'Position', 'LTP', 'Open P&L', 'Today W/L', 'Today net', 'All-time W/L', 'All-time net', 'All-time + open'].map(label => <th key={label} className="p-3 font-medium uppercase tracking-wide">{label}</th>)}</tr></thead>
        <tbody>{data?.timeframes.map(row => <tr key={row.minutes} className="border-t border-[#1f2937] text-gray-200">
          <td className="p-3"><div className="font-semibold text-[#93c5fd]">{row.label.replace('Delta Gold ', '')}</div><div className="mt-1 text-gray-500">{row.minutes}m engine</div></td>
          <td className="p-3"><span className={row.scan_enabled ? 'text-[#22c55e]' : 'text-gray-500'}>Scan {row.scan_enabled ? 'ON' : 'OFF'}</span><br/><span className={row.trading_enabled ? 'text-[#22c55e]' : 'text-gray-500'}>Trade {row.trading_enabled ? 'ON' : 'OFF'}</span></td>
          <td className="p-3">{row.cooldown?.active ? <><span className="text-[#f59e0b]">RESTING {rest(row.cooldown.remaining_seconds)}</span><div className="mt-1 text-gray-500">until {time(row.cooldown.until)}</div></> : <span className="text-[#22c55e]">READY</span>}</td>
          <td className="p-3"><span className={row.stale ? 'text-[#f59e0b]' : 'text-[#22c55e]'}>{row.stale ? 'STALE' : 'FRESH'}</span><div className="mt-1 font-mono text-gray-500">{time(row.last_bar_at)}</div></td>
          <td className="p-3 font-mono">{number(row.ema20)}</td><td className="p-3 font-mono">{number(row.volume_ema20)}</td>
          <td className="p-3">{row.position ? <><span className={row.position.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}>{row.position.side}</span><div className="mt-1 font-mono">@ {number(row.position.entry_price)}</div></> : <span className="text-gray-500">FLAT</span>}</td>
          <td className="p-3 font-mono">{number(data.ltp)}</td><MoneyCell value={row.unrealized} inr={inr(row.unrealized)} /><td className="p-3 font-mono">{row.today.wins}/{row.today.losses}<div className="mt-1 text-gray-500">{number(row.today.trades, 0)} trades</div></td>
          <MoneyCell value={row.today.net} inr={inr(row.today.net)} detail={`G ${number(row.today.gross)} | F ${number(row.today.fees)}`} /><td className="p-3 font-mono">{row.all_time.wins}/{row.all_time.losses}<div className="mt-1 text-gray-500">{number(row.all_time.win_rate)}%</div></td>
          <MoneyCell value={row.all_time.net} inr={inr(row.all_time.net)} detail={`G ${number(row.all_time.gross)} | F ${number(row.all_time.fees)}`} /><MoneyCell value={row.combined_all_time} inr={inr(row.combined_all_time)} />
        </tr>)}</tbody>
      </table>
      {data && !data.timeframes.length && <p className="p-5 text-sm text-gray-500">No Delta strategy timeframes are enabled for this deployment.</p>}
    </div>
    <p className="rounded border border-[#f59e0b]/30 bg-[#f59e0b]/5 p-3 text-xs text-[#fbbf24]">Combined values add independent paper simulations. Several timeframes may hold overlapping PAXG positions, so this is not a single-account portfolio result.</p>
  </section>;
}

function Summary({ label, value, inr, detail }: { label: string; value?: number; inr: string; detail: string }) {
  return <div className="rounded border border-[#1f2937] bg-[linear-gradient(145deg,#111827,#0d131e)] p-4"><div className="text-xs uppercase tracking-wide text-gray-400">{label}</div><div className={`mt-3 font-mono text-xl ${tone(value)}`}>{number(value)}</div><div className="mt-1 font-mono text-sm text-gray-400">{inr}</div><p className="mt-3 text-xs text-gray-500">{detail}</p></div>;
}

function MoneyCell({ value, inr, detail }: { value: number; inr: string; detail?: string }) {
  return <td className={`p-3 font-mono ${tone(value)}`}>{number(value)}<div className="mt-1 text-[11px] text-gray-500">{inr}</div>{detail && <div className="mt-1 text-[10px] text-gray-600">{detail}</div>}</td>;
}
