'use client';

import { useEffect, useState } from 'react';
import { deltaApi, DeltaAsset } from '../lib/api';

type Period = { trades: number; wins: number; losses: number; breakeven: number; win_rate: number; gross: number; fees: number; net: number };
type Outcome = {
  minutes: number; label: string; scan_enabled?: boolean; trading_enabled?: boolean; stale: boolean;
  last_bar_at?: number; ema20?: number; volume_ema20?: number; unrealized: number;
  combined_today: number; combined_all_time: number; today: Period; all_time: Period;
  cooldown?: { active: boolean; until?: number; remaining_seconds: number; reason?: string };
  position?: { side: string; entry_price: number; pnl_inr?: number };
};
type Overview = {
  symbol?: string; generated_at: number; utc_date: string; ltp?: number; currency?: string; inr_rate?: number;
  timeframes: Outcome[];
  totals: { today: Period; all_time: Period; unrealized: number; today_with_unrealized: number; all_time_with_unrealized: number };
};

const number = (value?: number, digits = 2) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: digits });
const tone = (value?: number) => (value || 0) > 0 ? 'text-[#22c55e]' : (value || 0) < 0 ? 'text-[#ef4444]' : 'text-gray-300';
const time = (value?: number) => value ? new Date(value * 1000).toLocaleString('en-IN', { timeZone: 'UTC', hour12: false }) : '--';
const rest = (seconds?: number) => `${Math.floor((seconds || 0) / 60)}m ${Math.ceil((seconds || 0) % 60)}s`;

export default function DeltaOverviewTab({ asset = 'gold', mode = 'paper', viewerMode = false }: { asset?: DeltaAsset; mode?: 'paper' | 'live'; viewerMode?: boolean }) {
  const api = deltaApi(asset, mode);
  const metal = asset === 'silver' ? 'Silver' : 'Gold';
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    setData(null);
    setError('');
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
  }, [asset, mode]);
  const currency = data?.currency || 'USD';
  const inr = (value?: number) => data?.inr_rate && value != null ? `Rs ${number(value * data.inr_rate)}` : '--';
  const frames = data?.timeframes || [];
  const maxOutcome = Math.max(1, ...frames.map(row => Math.abs(row.combined_today)));
  return <section className="space-y-4">
    <header className="relative overflow-hidden rounded-xl border border-[#1d3850] p-5" style={{ background: 'radial-gradient(circle at 82% 12%, var(--accent-hero-glow), transparent 28%), linear-gradient(135deg, var(--accent-hero-from), var(--accent-hero-to) 70%)' }}>
      <div className="absolute -right-12 -top-16 h-40 w-40 rounded-full border border-[#38bdf8]/10" />
      <div className="relative flex flex-wrap items-end justify-between gap-4">
        <div><p className="text-[10px] uppercase tracking-[0.3em] text-[#38bdf8]">{metal} {mode} dashboard</p><h1 className="mt-2 text-2xl font-semibold text-gray-100">Delta {metal} overview</h1><p className="mt-1 text-sm text-gray-400">{frames.length} enabled {asset} timeframes. Daily totals reset at 00:00 UTC.</p></div>
        <div className="text-right"><div className="font-mono text-2xl text-gray-100">{number(data?.ltp)}</div><div className="mt-1 text-[10px] uppercase tracking-widest text-gray-500">{data?.symbol || metal} last trade</div><div className="mt-2 text-xs text-gray-500">Updated {data ? time(data.generated_at) : '--'} UTC</div></div>
      </div>
    </header>
    {error && <p role="alert" className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 p-3 text-sm text-[#f87171]">{error}</p>}
    {!data && !error && <p className="rounded border border-[#1f2937] bg-[#0d131e] p-3 text-sm text-gray-400">Loading Delta overview data...</p>}
    <div className="grid gap-3 xl:grid-cols-[0.36fr_1.64fr]">
      <div className="rounded-xl border border-[#1f2937] bg-[#0d131e] p-4">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2"><div><p className="text-[10px] uppercase tracking-[0.2em] text-gray-500">Relative contribution</p><h2 className="mt-1 text-sm font-semibold text-gray-200">Today realized + open</h2></div><p className="text-[10px] uppercase tracking-wide text-gray-600">{mode} view</p></div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">{frames.map(row => <OutcomeBar key={row.minutes} row={row} max={maxOutcome} currency={currency} mode={mode} />)}</div>
        {data && !frames.length && <p className="text-sm text-gray-500">No enabled timeframes.</p>}
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">{frames.map(row => <TimeframeCard key={row.minutes} row={row} ltp={data?.ltp} currency={currency} inr={inr} viewerMode={viewerMode} />)}</div>
    </div>
    {!viewerMode && <details className="overflow-hidden rounded-xl border border-[#1f2937] bg-[#0d131e]">
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-[#93c5fd] hover:bg-[#111827]">Detailed metrics and exact values</summary>
      <div className="overflow-x-auto border-t border-[#1f2937]">
      <table className="w-full min-w-[1320px] text-left text-xs">
        <thead className="bg-[#111827] text-gray-400"><tr>{['Timeframe', 'Controls', 'Entry rest', 'Feed / last bar UTC', 'EMA20', 'Volume EMA20', 'Position', 'LTP', 'Open P&L', 'Today W/L', 'Today net', 'All-time W/L', 'All-time net', 'All-time + open'].map(label => <th key={label} className="p-3 font-medium uppercase tracking-wide">{label}</th>)}</tr></thead>
        <tbody>{data?.timeframes.map(row => <tr key={row.minutes} className="border-t border-[#1f2937] text-gray-200">
          <td className="p-3"><div className="font-semibold text-[#93c5fd]">{row.label.replace(/^Delta (Gold|Silver) /, '')}</div><div className="mt-1 text-gray-500">{row.minutes}m engine</div></td>
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
    </details>}
    <p className="rounded border border-[#f59e0b]/30 bg-[#f59e0b]/5 p-3 text-xs text-[#fbbf24]">Combined values add independent paper simulations. Several timeframes may hold overlapping {asset} positions, so this is not a single-account portfolio result.</p>
  </section>;
}

function OutcomeBar({ row, max, currency, mode }: { row: Outcome; max: number; currency: string; mode: 'paper' | 'live' }) {
  const width = `${Math.max(row.combined_today === 0 ? 0 : 3, Math.abs(row.combined_today) / max * 50)}%`;
  const positive = row.combined_today >= 0;
  const hasOpenTrade = mode === 'live' && !!row.position;
  const openTradePositive = row.unrealized >= 0;
  const livePositionTone = hasOpenTrade ? (openTradePositive ? 'text-[#22c55e]' : 'text-[#ef4444]') : 'text-[#93c5fd]';
  const blinkClass = hasOpenTrade
    ? (openTradePositive ? 'delta-trade-blink-green' : 'delta-trade-blink-red')
    : '';
  return <div><div className="mb-1 flex items-center justify-between gap-3 text-xs"><span className={`inline-flex items-center gap-1.5 font-semibold ${livePositionTone}`}>{hasOpenTrade && <span className={`h-2 w-2 rounded-full ${blinkClass}`} aria-hidden="true" />}{row.label.replace(/^Delta (Gold|Silver) /, '')}</span><span className={`shrink-0 font-mono ${tone(row.combined_today)}`}>{number(row.combined_today)} {currency}</span></div><div className="relative h-1.5 overflow-hidden rounded-full bg-[#182231]"><div className="absolute left-1/2 top-0 h-full w-px bg-gray-500/40" /><div className={`absolute top-0 h-full rounded-full ${positive ? 'left-1/2 bg-[#22c55e]' : 'right-1/2 bg-[#ef4444]'}`} style={{ width }} /></div></div>;
}

function TimeframeCard({ row, ltp, currency, inr, viewerMode = false }: { row: Outcome; ltp?: number; currency: string; inr: (value?: number) => string; viewerMode?: boolean }) {
  const positionColor = row.position?.side === 'BUY' ? '#22c55e' : row.position?.side === 'SELL' ? '#ef4444' : '#334155';
  return <article className="relative overflow-hidden rounded-xl border border-[#1f2937] bg-[linear-gradient(145deg,#101824,#0b111a)] p-4">
    <div className="absolute left-0 top-0 h-1 w-full" style={{ background: positionColor }} />
    <div className="flex items-start justify-between gap-3"><div><div className="text-lg font-semibold text-gray-100">{row.label.replace(/^Delta (Gold|Silver) /, '')}</div><div className="mt-1 flex gap-2 text-[10px] uppercase tracking-wide"><span className={row.stale ? 'text-[#f59e0b]' : 'text-[#22c55e]'}>{row.stale ? 'Stale feed' : 'Fresh feed'}</span><span className={row.cooldown?.active ? 'text-[#f59e0b]' : 'text-gray-500'}>{row.cooldown?.active ? `Rest ${rest(row.cooldown.remaining_seconds)}` : 'Entry ready'}</span></div></div><div className={`rounded-md border px-2 py-1 text-xs font-semibold ${row.position?.side === 'BUY' ? 'border-[#22c55e]/40 bg-[#22c55e]/10 text-[#22c55e]' : row.position?.side === 'SELL' ? 'border-[#ef4444]/40 bg-[#ef4444]/10 text-[#ef4444]' : 'border-[#334155] text-gray-500'}`}>{row.position?.side || 'FLAT'}</div></div>
    <div className="mt-4 flex items-end justify-between gap-3"><div><div className="text-[10px] uppercase tracking-wide text-gray-500">Today + open</div><div className={`mt-1 font-mono text-2xl ${tone(row.combined_today)}`}>{number(row.combined_today)} <span className="text-xs text-gray-500">{currency}</span></div><div className="mt-1 text-xs text-gray-500">{inr(row.combined_today)}</div></div><div className="text-right text-xs text-gray-500">{row.position ? <>Entry <span className="font-mono text-gray-300">{number(row.position.entry_price)}</span><br/>LTP <span className="font-mono text-gray-300">{number(ltp)}</span></> : 'No open exposure'}</div></div>
    <div className="mt-4 grid grid-cols-3 gap-2 border-t border-[#1f2937] pt-3 text-xs"><Mini label="Today W/L" value={`${row.today.wins}/${row.today.losses}`} /><Mini label="All-time net" value={number(row.all_time.net)} valueClass={tone(row.all_time.net)} /><Mini label="Win rate" value={`${number(row.all_time.win_rate)}%`} /></div>
    {!viewerMode && <div className="mt-3 flex justify-between text-[10px] text-gray-600"><span>EMA {number(row.ema20)}</span><span>{row.scan_enabled && row.trading_enabled ? 'Active' : row.scan_enabled ? 'Scan only' : 'Disabled'}</span></div>}
  </article>;
}

function Mini({ label, value, valueClass = 'text-gray-200' }: { label: string; value: string; valueClass?: string }) {
  return <div><div className={`font-mono ${valueClass}`}>{value}</div><div className="mt-1 text-[9px] uppercase tracking-wide text-gray-600">{label}</div></div>;
}

function MoneyCell({ value, inr, detail }: { value: number; inr: string; detail?: string }) {
  return <td className={`p-3 font-mono ${tone(value)}`}>{number(value)}<div className="mt-1 text-[11px] text-gray-500">{inr}</div>{detail && <div className="mt-1 text-[10px] text-gray-600">{detail}</div>}</td>;
}
