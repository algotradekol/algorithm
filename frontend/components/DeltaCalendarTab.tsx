'use client';

import { useEffect, useMemo, useState } from 'react';
import { DeltaAsset, deltaApi } from '../lib/api';

type CalendarTrade = {
  minutes: number;
  symbol: string;
  side: string;
  entry_time: string | null;
  exit_time: string | null;
  entry_price: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  pnl_inr: number | null;
};
type CalendarDay = {
  date: string;
  trades: CalendarTrade[];
  wins: number;
  losses: number;
  net_pnl_inr: number;
};

const INR = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });
const WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

function istTime(iso: string | null) {
  if (!iso) return '--';
  try {
    return new Date(iso).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

// Same convention as the top-of-tab labels: > 60 min renders as hours.
function tfLabel(minutes: number) {
  if (minutes % 60 === 0 && minutes >= 60) return `${minutes / 60} hr`;
  return `${minutes} min`;
}

function longDate(iso: string) {
  try {
    // Interpret the IST-day key as noon IST so the local formatter doesn't
    // shift it into the previous calendar day for viewers west of IST.
    const d = new Date(`${iso}T12:00:00+05:30`);
    return d.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  } catch { return iso; }
}

function daysGrid(year: number, month: number) {
  const first = new Date(Date.UTC(year, month - 1, 1));
  const firstWeekday = (first.getUTCDay() + 6) % 7; // Monday = 0
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const cells: (string | null)[] = [];
  for (let i = 0; i < firstWeekday; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) {
    const iso = `${year}-${String(month).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    cells.push(iso);
  }
  while (cells.length % 7) cells.push(null);
  return cells;
}

export default function DeltaCalendarTab({ asset, mode }: { asset: DeltaAsset; mode: 'paper' | 'live' }) {
  const now = new Date();
  const [year, setYear] = useState(now.getUTCFullYear());
  const [month, setMonth] = useState(now.getUTCMonth() + 1);
  const [days, setDays] = useState<Record<string, CalendarDay> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setDays(null); setError(null);
    deltaApi(asset, mode).deltaCalendar(year, month)
      .then((data: { days: CalendarDay[] }) => {
        if (cancelled) return;
        const map: Record<string, CalendarDay> = {};
        for (const d of data.days) map[d.date] = d;
        setDays(map);
      })
      .catch((err: Error) => { if (!cancelled) setError(err.message || 'Calendar fetch failed'); });
    return () => { cancelled = true; };
  }, [asset, mode, year, month]);
  const cells = useMemo(() => daysGrid(year, month), [year, month]);
  const monthTotal = useMemo(() => {
    if (!days) return 0;
    return Object.values(days).reduce((acc, d) => acc + (d.net_pnl_inr || 0), 0);
  }, [days]);
  function shiftMonth(delta: number) {
    const next = new Date(Date.UTC(year, month - 1 + delta, 1));
    setYear(next.getUTCFullYear()); setMonth(next.getUTCMonth() + 1); setSelectedDate(null);
  }
  const selectedDay = selectedDate && days ? days[selectedDate] : null;
  useEffect(() => {
    if (!selectedDay) return;
    function onKey(e: KeyboardEvent) { if (e.key === 'Escape') setSelectedDate(null); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedDay]);
  return <div className="space-y-3">
    <div className="panel flex items-center justify-between p-3">
      <div className="flex items-center gap-2">
        <button onClick={() => shiftMonth(-1)} className="rounded border border-[#334155] px-2 py-1 text-sm text-gray-300 hover:text-gray-100" aria-label="Previous month">◀</button>
        <span className="min-w-[10ch] text-center text-sm font-semibold text-gray-100">{MONTH_NAMES[month - 1]} {year}</span>
        <button onClick={() => shiftMonth(1)} className="rounded border border-[#334155] px-2 py-1 text-sm text-gray-300 hover:text-gray-100" aria-label="Next month">▶</button>
      </div>
      <div className="text-sm">
        <span className="text-gray-500">Month total (INR): </span>
        <span className={monthTotal >= 0 ? 'font-semibold text-[#4ade80]' : 'font-semibold text-[#f87171]'}>{monthTotal >= 0 ? '+' : ''}₹{INR.format(monthTotal)}</span>
        <span className="ml-3 text-[11px] uppercase tracking-wide text-gray-500">{mode}</span>
      </div>
    </div>
    {error && <p role="alert" className="panel p-3 text-sm text-[#f87171]">{error}</p>}
    <div className="panel p-3">
      <div className="grid grid-cols-7 gap-1 text-[11px] uppercase tracking-wide text-gray-500">
        {WEEK.map(day => <div key={day} className="px-2 py-1 text-center">{day}</div>)}
      </div>
      <div className="mt-1 grid grid-cols-7 gap-1">
        {cells.map((iso, index) => {
          if (!iso) return <div key={`empty-${index}`} className="h-20 rounded border border-transparent" />;
          const day = days?.[iso];
          const dayNum = Number(iso.slice(-2));
          const isSelected = iso === selectedDate;
          const hasTrades = !!day && day.trades.length > 0;
          const pnl = day?.net_pnl_inr ?? 0;
          const tone = !hasTrades ? 'border-[#1f2937] text-gray-500'
            : pnl > 0 ? 'border-[#22c55e]/50 bg-[#22c55e]/10 text-[#bbf7d0]'
            : pnl < 0 ? 'border-[#ef4444]/50 bg-[#ef4444]/10 text-[#fecaca]'
            : 'border-[#334155] text-gray-300';
          return <button key={iso} onClick={() => hasTrades && setSelectedDate(iso === selectedDate ? null : iso)}
            className={`h-20 rounded border p-2 text-left ${tone} ${isSelected ? 'ring-2 ring-[#3b82f6]' : ''} ${hasTrades ? 'cursor-pointer hover:brightness-125' : 'cursor-default'}`}>
            <div className="flex items-center justify-between text-xs font-semibold">
              <span>{dayNum}</span>
              {hasTrades && <span className="text-[10px] text-gray-400">{day!.trades.length}</span>}
            </div>
            {hasTrades && <div className="mt-1 text-xs">
              <div className={pnl >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}>{pnl >= 0 ? '+' : ''}₹{INR.format(pnl)}</div>
              <div className="text-[10px] text-gray-500">{day!.wins}W · {day!.losses}L</div>
            </div>}
          </button>;
        })}
      </div>
    </div>
    {selectedDay && <DayModal day={selectedDay} onClose={() => setSelectedDate(null)} />}
  </div>;
}

function DayModal({ day, onClose }: { day: CalendarDay; onClose: () => void }) {
  const pnl = day.net_pnl_inr || 0;
  const winRate = day.trades.length > 0 ? Math.round((day.wins / day.trades.length) * 100) : 0;
  const best = day.trades.reduce((a, t) => Math.max(a, t.pnl_inr ?? -Infinity), -Infinity);
  const worst = day.trades.reduce((a, t) => Math.min(a, t.pnl_inr ?? Infinity), Infinity);
  const maxAbs = Math.max(Math.abs(best === -Infinity ? 0 : best), Math.abs(worst === Infinity ? 0 : worst), 1);
  // Sort by exit_time desc so the most recent trade is on top.
  const trades = [...day.trades].sort((a, b) => (b.exit_time || '').localeCompare(a.exit_time || ''));
  return <div role="dialog" aria-modal="true" aria-label={`Trades on ${day.date}`}
    onClick={onClose}
    className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
    <div onClick={e => e.stopPropagation()}
      className="flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-[#1f2937] bg-[#0b1220] shadow-2xl">
      <header className="border-b border-[#1f2937] bg-gradient-to-r from-[#0f172a] to-[#111827] px-5 py-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-[11px] uppercase tracking-widest text-gray-500">Trading day</div>
            <h3 className="mt-0.5 text-lg font-semibold text-gray-100">{longDate(day.date)}</h3>
          </div>
          <button onClick={onClose} aria-label="Close" className="rounded-full border border-[#334155] p-1.5 text-gray-400 hover:border-gray-500 hover:text-gray-100">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
          </button>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <StatBlock label="Net P&L" value={`${pnl >= 0 ? '+' : ''}₹${INR.format(pnl)}`} tone={pnl >= 0 ? 'good' : 'bad'} />
          <StatBlock label="Trades" value={String(day.trades.length)} tone="neutral" />
          <StatBlock label="Win rate" value={`${winRate}%`} tone={winRate >= 50 ? 'good' : 'bad'} sub={`${day.wins}W · ${day.losses}L`} />
          <StatBlock label="Best / Worst"
            value={`${best === -Infinity ? '--' : `₹${INR.format(best)}`} · ${worst === Infinity ? '--' : `₹${INR.format(worst)}`}`}
            tone="neutral" small />
        </div>
      </header>
      <div className="flex-1 overflow-y-auto p-4">
        <ol className="space-y-2">
          {trades.map((trade, index) => {
            const p = trade.pnl_inr ?? 0;
            const positive = p >= 0;
            const barPct = Math.min(100, Math.round((Math.abs(p) / maxAbs) * 100));
            return <li key={index} className={`overflow-hidden rounded-lg border ${positive ? 'border-[#22c55e]/30 bg-[#22c55e]/5' : 'border-[#ef4444]/30 bg-[#ef4444]/5'}`}>
              <div className="grid grid-cols-12 items-center gap-3 px-3 py-2">
                <div className="col-span-2 flex items-center gap-2">
                  <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${trade.side === 'BUY' ? 'bg-[#22c55e]/20 text-[#4ade80]' : 'bg-[#ef4444]/20 text-[#f87171]'}`}>{trade.side}</span>
                  <span className="rounded bg-[#1f2937] px-1.5 py-0.5 text-[10px] font-semibold text-gray-300">{tfLabel(trade.minutes)}</span>
                </div>
                <div className="col-span-5 text-xs text-gray-400">
                  <span className="text-gray-500">{istTime(trade.entry_time)}</span>
                  <span className="mx-2 text-gray-600">→</span>
                  <span className="text-gray-300">{istTime(trade.exit_time)}</span>
                  {trade.exit_reason && <span className="ml-2 rounded bg-[#111827] px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-gray-400">{trade.exit_reason}</span>}
                </div>
                <div className="col-span-3 text-xs text-gray-500">
                  {trade.entry_price != null && <>@ {trade.entry_price}
                    {trade.exit_price != null && <span className="text-gray-600"> → {trade.exit_price}</span>}</>}
                </div>
                <div className={`col-span-2 text-right text-sm font-semibold ${positive ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
                  {trade.pnl_inr != null ? `${positive ? '+' : ''}₹${INR.format(p)}` : '--'}
                </div>
              </div>
              <div className="h-1 w-full bg-[#0a0e14]">
                <div className={`h-full ${positive ? 'bg-[#4ade80]' : 'bg-[#f87171]'}`} style={{ width: `${barPct}%` }} />
              </div>
            </li>;
          })}
        </ol>
      </div>
      <footer className="border-t border-[#1f2937] px-5 py-2 text-[11px] text-gray-500">
        Times are IST · P&L converted at Delta India's ₹85/USD reference rate · Press <kbd className="rounded border border-[#334155] bg-[#111827] px-1 text-gray-400">Esc</kbd> to close
      </footer>
    </div>
  </div>;
}

function StatBlock({ label, value, tone, sub, small }: { label: string; value: string; tone: 'good' | 'bad' | 'neutral'; sub?: string; small?: boolean }) {
  const valueTone = tone === 'good' ? 'text-[#4ade80]' : tone === 'bad' ? 'text-[#f87171]' : 'text-gray-100';
  return <div className="rounded-md border border-[#1f2937] bg-[#0a0e14] px-3 py-2">
    <div className="text-[10px] uppercase tracking-widest text-gray-500">{label}</div>
    <div className={`mt-0.5 font-semibold ${small ? 'text-xs' : 'text-base'} ${valueTone}`}>{value}</div>
    {sub && <div className="mt-0.5 text-[10px] text-gray-500">{sub}</div>}
  </div>;
}
