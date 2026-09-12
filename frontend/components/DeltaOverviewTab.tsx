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
  const frames = data?.timeframes || [];
  const openPositions = frames.filter(row => row.position).length;
  const freshFeeds = frames.filter(row => !row.stale).length;
  const resting = frames.filter(row => row.cooldown?.active).length;
  const maxOutcome = Math.max(1, ...frames.map(row => Math.abs(row.combined_today)));
  const freshPercent = frames.length ? Math.round(freshFeeds / frames.length * 100) : 0;
  return <section className="space-y-4">
    <header className="relative overflow-hidden rounded-xl border border-[#1d3850] bg-[radial-gradient(circle_at_82%_12%,rgba(14,165,233,0.14),transparent_28%),linear-gradient(135deg,#0d1824,#091018_70%)] p-5">
      <div className="absolute -right-12 -top-16 h-40 w-40 rounded-full border border-[#38bdf8]/10" />
      <div className="relative flex flex-wrap items-end justify-between gap-4">
        <div><p className="text-[10px] uppercase tracking-[0.3em] text-[#38bdf8]">PAXG paper command center</p><h1 className="mt-2 text-2xl font-semibold text-gray-100">Delta strategy overview</h1><p className="mt-1 text-sm text-gray-400">Six independent time horizons, one operational picture. Daily totals reset at 00:00 UTC.</p></div>
        <div className="text-right"><div className="font-mono text-2xl text-gray-100">{number(data?.ltp)}</div><div className="mt-1 text-[10px] uppercase tracking-widest text-gray-500">PAXGUSD last trade</div><div className="mt-2 text-xs text-gray-500">Updated {data ? time(data.generated_at) : '--'} UTC</div></div>
      </div>
    </header>
    {error && <p role="alert" className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 p-3 text-sm text-[#f87171]">{error}</p>}
    <div className="grid gap-3 xl:grid-cols-[1.45fr_1fr]">
      <div className="grid gap-3 sm:grid-cols-2">
        <Summary featured label={`Today + open (${currency})`} value={data?.totals.today_with_unrealized} inr={inr(data?.totals.today_with_unrealized)} detail={`${number(data?.totals.today.trades, 0)} closed today | ${openPositions} open now`} />
        <Summary label={`Today realized (${currency})`} value={data?.totals.today.net} inr={inr(data?.totals.today.net)} detail={`${number(data?.totals.today.wins, 0)} wins / ${number(data?.totals.today.losses, 0)} losses | ${number(data?.totals.today.win_rate)}%`} />
        <Summary label={`Open P&L (${currency})`} value={data?.totals.unrealized} inr={inr(data?.totals.unrealized)} detail="Live mark across open simulations" />
        <Summary label={`All-time + open (${currency})`} value={data?.totals.all_time_with_unrealized} inr={inr(data?.totals.all_time_with_unrealized)} detail={`${number(data?.totals.all_time.trades, 0)} all-time closed trades`} />
      </div>
      <div className="rounded-xl border border-[#1f2937] bg-[#0d131e] p-4">
        <div className="flex items-center justify-between"><div><p className="text-[10px] uppercase tracking-[0.2em] text-gray-500">System pulse</p><h2 className="mt-1 text-sm font-semibold text-gray-200">Strategy health</h2></div><div className="grid h-20 w-20 place-items-center rounded-full" style={{ background: `conic-gradient(#22c55e ${freshPercent}%, #1f2937 0)` }}><div className="grid h-14 w-14 place-items-center rounded-full bg-[#0d131e] font-mono text-sm text-gray-100">{freshPercent}%</div></div></div>
        <div className="mt-4 grid grid-cols-2 gap-2">
          <Pulse label="Fresh feeds" value={`${freshFeeds}/${frames.length}`} color="text-[#22c55e]" />
          <Pulse label="Open positions" value={String(openPositions)} color="text-[#38bdf8]" />
          <Pulse label="Entry rest" value={String(resting)} color={resting ? 'text-[#f59e0b]' : 'text-gray-300'} />
          <Pulse label="Trading on" value={`${frames.filter(row => row.trading_enabled).length}/${frames.length}`} color="text-[#22c55e]" />
        </div>
      </div>
    </div>
    <div className="grid gap-3 lg:grid-cols-[0.9fr_1.6fr]">
      <div className="rounded-xl border border-[#1f2937] bg-[#0d131e] p-4">
        <div className="mb-4"><p className="text-[10px] uppercase tracking-[0.2em] text-gray-500">Relative contribution</p><h2 className="mt-1 text-sm font-semibold text-gray-200">Today realized + open</h2></div>
        <div className="space-y-4">{frames.map(row => <OutcomeBar key={row.minutes} row={row} max={maxOutcome} currency={currency} />)}</div>
        {!frames.length && <p className="text-sm text-gray-500">No enabled timeframes.</p>}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">{frames.map(row => <TimeframeCard key={row.minutes} row={row} ltp={data?.ltp} currency={currency} inr={inr} />)}</div>
    </div>
    <details className="overflow-hidden rounded-xl border border-[#1f2937] bg-[#0d131e]">
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-[#93c5fd] hover:bg-[#111827]">Detailed metrics and exact values</summary>
      <div className="overflow-x-auto border-t border-[#1f2937]">
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
    </details>
    <p className="rounded border border-[#f59e0b]/30 bg-[#f59e0b]/5 p-3 text-xs text-[#fbbf24]">Combined values add independent paper simulations. Several timeframes may hold overlapping PAXG positions, so this is not a single-account portfolio result.</p>
  </section>;
}

function Summary({ label, value, inr, detail, featured = false }: { label: string; value?: number; inr: string; detail: string; featured?: boolean }) {
  return <div className={`relative overflow-hidden rounded-xl border p-4 ${featured ? 'border-[#0ea5e9]/50 bg-[linear-gradient(145deg,rgba(14,165,233,0.13),#0d131e_62%)]' : 'border-[#1f2937] bg-[linear-gradient(145deg,#111827,#0d131e)]'}`}><div className={`absolute left-0 top-0 h-full w-1 ${featured ? 'bg-[#38bdf8]' : (value || 0) >= 0 ? 'bg-[#22c55e]/60' : 'bg-[#ef4444]/70'}`} /><div className="text-[10px] uppercase tracking-[0.16em] text-gray-400">{label}</div><div className={`mt-3 font-mono ${featured ? 'text-3xl' : 'text-2xl'} ${tone(value)}`}>{number(value)}</div><div className="mt-1 font-mono text-sm text-gray-400">{inr}</div><p className="mt-3 text-xs text-gray-500">{detail}</p></div>;
}

function Pulse({ label, value, color }: { label: string; value: string; color: string }) {
  return <div className="rounded-lg border border-[#1f2937] bg-[#0a1018] p-3"><div className={`font-mono text-lg ${color}`}>{value}</div><div className="mt-1 text-[10px] uppercase tracking-wide text-gray-500">{label}</div></div>;
}

function OutcomeBar({ row, max, currency }: { row: Outcome; max: number; currency: string }) {
  const width = `${Math.max(row.combined_today === 0 ? 0 : 3, Math.abs(row.combined_today) / max * 50)}%`;
  const positive = row.combined_today >= 0;
  return <div><div className="mb-1.5 flex items-center justify-between text-xs"><span className="font-semibold text-[#93c5fd]">{row.label.replace('Delta Gold ', '')}</span><span className={`font-mono ${tone(row.combined_today)}`}>{number(row.combined_today)} {currency}</span></div><div className="relative h-2 overflow-hidden rounded-full bg-[#182231]"><div className="absolute left-1/2 top-0 h-full w-px bg-gray-500/40" /><div className={`absolute top-0 h-full rounded-full ${positive ? 'left-1/2 bg-[#22c55e]' : 'right-1/2 bg-[#ef4444]'}`} style={{ width }} /></div></div>;
}

function TimeframeCard({ row, ltp, currency, inr }: { row: Outcome; ltp?: number; currency: string; inr: (value?: number) => string }) {
  const positionColor = row.position?.side === 'BUY' ? '#22c55e' : row.position?.side === 'SELL' ? '#ef4444' : '#334155';
  return <article className="relative overflow-hidden rounded-xl border border-[#1f2937] bg-[linear-gradient(145deg,#101824,#0b111a)] p-4">
    <div className="absolute left-0 top-0 h-1 w-full" style={{ background: positionColor }} />
    <div className="flex items-start justify-between gap-3"><div><div className="text-lg font-semibold text-gray-100">{row.label.replace('Delta Gold ', '')}</div><div className="mt-1 flex gap-2 text-[10px] uppercase tracking-wide"><span className={row.stale ? 'text-[#f59e0b]' : 'text-[#22c55e]'}>{row.stale ? 'Stale feed' : 'Fresh feed'}</span><span className={row.cooldown?.active ? 'text-[#f59e0b]' : 'text-gray-500'}>{row.cooldown?.active ? `Rest ${rest(row.cooldown.remaining_seconds)}` : 'Entry ready'}</span></div></div><div className={`rounded-md border px-2 py-1 text-xs font-semibold ${row.position?.side === 'BUY' ? 'border-[#22c55e]/40 bg-[#22c55e]/10 text-[#22c55e]' : row.position?.side === 'SELL' ? 'border-[#ef4444]/40 bg-[#ef4444]/10 text-[#ef4444]' : 'border-[#334155] text-gray-500'}`}>{row.position?.side || 'FLAT'}</div></div>
    <div className="mt-4 flex items-end justify-between gap-3"><div><div className="text-[10px] uppercase tracking-wide text-gray-500">Today + open</div><div className={`mt-1 font-mono text-2xl ${tone(row.combined_today)}`}>{number(row.combined_today)} <span className="text-xs text-gray-500">{currency}</span></div><div className="mt-1 text-xs text-gray-500">{inr(row.combined_today)}</div></div><div className="text-right text-xs text-gray-500">{row.position ? <>Entry <span className="font-mono text-gray-300">{number(row.position.entry_price)}</span><br/>LTP <span className="font-mono text-gray-300">{number(ltp)}</span></> : 'No open exposure'}</div></div>
    <div className="mt-4 grid grid-cols-3 gap-2 border-t border-[#1f2937] pt-3 text-xs"><Mini label="Today W/L" value={`${row.today.wins}/${row.today.losses}`} /><Mini label="All-time net" value={number(row.all_time.net)} valueClass={tone(row.all_time.net)} /><Mini label="Win rate" value={`${number(row.all_time.win_rate)}%`} /></div>
    <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#182231]"><div className="h-full bg-[#38bdf8]" style={{ width: `${Math.min(100, Math.max(0, row.all_time.win_rate))}%` }} /></div>
    <div className="mt-3 flex justify-between text-[10px] text-gray-600"><span>EMA {number(row.ema20)}</span><span>Vol EMA {number(row.volume_ema20)}</span><span>{row.scan_enabled && row.trading_enabled ? 'Active' : row.scan_enabled ? 'Scan only' : 'Disabled'}</span></div>
  </article>;
}

function Mini({ label, value, valueClass = 'text-gray-200' }: { label: string; value: string; valueClass?: string }) {
  return <div><div className={`font-mono ${valueClass}`}>{value}</div><div className="mt-1 text-[9px] uppercase tracking-wide text-gray-600">{label}</div></div>;
}

function MoneyCell({ value, inr, detail }: { value: number; inr: string; detail?: string }) {
  return <td className={`p-3 font-mono ${tone(value)}`}>{number(value)}<div className="mt-1 text-[11px] text-gray-500">{inr}</div>{detail && <div className="mt-1 text-[10px] text-gray-600">{detail}</div>}</td>;
}
