'use client';

import { useEffect, useMemo, useState } from 'react';
import { DeltaAsset, deltaApi } from '../lib/api';
import { TradeTable, Trade } from './DeltaTab';

type CalendarTrade = Trade & { minutes: number };
type CalendarDay = {
  date: string;
  trades: CalendarTrade[];
  wins: number;
  losses: number;
  net_pnl_inr: number;
};
type CalendarResponse = {
  year: number;
  month: number;
  asset: DeltaAsset;
  mode: 'paper' | 'live';
  previous_trade: CalendarTrade | null;
  days: CalendarDay[];
};

const INR = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });
const WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

function tfLabel(minutes: number) {
  if (minutes % 60 === 0 && minutes >= 60) return `${minutes / 60} hr`;
  return `${minutes} min`;
}

function longDate(iso: string) {
  try {
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

function csvEscape(value: unknown) {
  if (value === null || value === undefined) return '';
  const text = String(value);
  if (/[",\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function downloadClosedCsv(day: CalendarDay, mode: 'paper' | 'live') {
  const headers = ['Timeframe', 'Side', 'Lots', 'Entry time (IST)', 'Entry', 'Reference time (IST)', 'Reference', 'Initial SL', 'Current SL', 'Target', 'Exit time (IST)', 'Exit', 'Reason', 'Gross', 'Net', 'Net (INR)'];
  const rows = day.trades.map(row => [
    tfLabel(row.minutes),
    row.side,
    row.qty,
    row.entry_time,
    row.entry_price,
    row.signal_snapshot?.setup_time || '',
    row.signal_snapshot?.setup_close ?? '',
    row.initial_sl,
    row.sl_price,
    row.target_price,
    row.exit_time || '',
    row.exit_price ?? '',
    row.exit_reason || '',
    row.gross_pnl ?? '',
    row.net_pnl ?? '',
    row.pnl_inr ?? '',
  ]);
  const csv = [headers, ...rows].map(cols => cols.map(csvEscape).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `delta-${mode}-closed-${day.date}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function DeltaCalendarTab({ asset, mode }: { asset: DeltaAsset; mode: 'paper' | 'live' }) {
  const now = new Date();
  const [year, setYear] = useState(now.getUTCFullYear());
  const [month, setMonth] = useState(now.getUTCMonth() + 1);
  const [payload, setPayload] = useState<CalendarResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setPayload(null); setError(null);
    deltaApi(asset, mode).deltaCalendar(year, month)
      .then((data: CalendarResponse) => { if (!cancelled) setPayload(data); })
      .catch((err: Error) => { if (!cancelled) setError(err.message || 'Calendar fetch failed'); });
    return () => { cancelled = true; };
  }, [asset, mode, year, month]);
  const dayMap = useMemo(() => {
    const map: Record<string, CalendarDay> = {};
    (payload?.days || []).forEach(d => { map[d.date] = d; });
    return map;
  }, [payload]);
  const cells = useMemo(() => daysGrid(year, month), [year, month]);
  const monthTotal = useMemo(() => {
    return Object.values(dayMap).reduce((acc, d) => acc + (d.net_pnl_inr || 0), 0);
  }, [dayMap]);
  function shiftMonth(delta: number) {
    const next = new Date(Date.UTC(year, month - 1 + delta, 1));
    setYear(next.getUTCFullYear()); setMonth(next.getUTCMonth() + 1); setSelectedDate(null);
  }
  const selectedDay = selectedDate ? dayMap[selectedDate] : null;
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
          const day = dayMap[iso];
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
    {selectedDay && <DayModal day={selectedDay} mode={mode}
      previousTrade={pickPrevious(payload, dayMap, selectedDay)}
      onClose={() => setSelectedDate(null)} />}
  </div>;
}

// The "+ last previous row" context in DeltaTab shows the trade immediately
// before the visible window. For the selected day, that's the newest trade
// with an earlier exit — either from an earlier day this month or, if this is
// the first traded day of the month, the month-boundary previous_trade the
// backend supplies.
function pickPrevious(payload: CalendarResponse | null, dayMap: Record<string, CalendarDay>, day: CalendarDay): CalendarTrade | null {
  const earlier = Object.values(dayMap).filter(d => d.date < day.date).sort((a, b) => b.date.localeCompare(a.date));
  const trade = earlier[0]?.trades?.[0];
  if (trade) return trade;
  return payload?.previous_trade || null;
}

function DayModal({ day, mode, previousTrade, onClose }: { day: CalendarDay; mode: 'paper' | 'live'; previousTrade: CalendarTrade | null; onClose: () => void }) {
  // Group trades by timeframe so the drill-down mirrors the per-timeframe
  // tabs' closed-trades table exactly, one section per TF, newest first.
  const grouped = useMemo(() => {
    const map = new Map<number, CalendarTrade[]>();
    for (const trade of day.trades) {
      if (!map.has(trade.minutes)) map.set(trade.minutes, []);
      map.get(trade.minutes)!.push(trade);
    }
    return Array.from(map.entries()).sort((a, b) => a[0] - b[0]);
  }, [day]);
  return <div role="dialog" aria-modal="true" aria-label={`Trades on ${day.date}`}
    onClick={onClose}
    className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
    <div onClick={e => e.stopPropagation()}
      className="flex max-h-[92vh] w-full max-w-6xl flex-col overflow-hidden rounded-xl border border-[#1f2937] bg-[#0b1220] shadow-2xl">
      <header className="flex items-center justify-between border-b border-[#1f2937] px-5 py-3">
        <h3 className="text-base font-semibold text-gray-100">{longDate(day.date)}</h3>
        <button onClick={onClose} aria-label="Close" className="rounded-full border border-[#334155] p-1.5 text-gray-400 hover:border-gray-500 hover:text-gray-100">
          <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
        </button>
      </header>
      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        {grouped.map(([minutes, trades]) => {
          const rowsForTable: Trade[] = previousTrade ? [...trades, previousTrade] : trades;
          return <section key={minutes} className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-sm font-semibold text-gray-200">
                CLOSED {mode.toUpperCase()} TRADES · {tfLabel(minutes)}
                {previousTrade && <span className="ml-2 text-xs font-normal text-gray-500">+ last previous row</span>}
              </h4>
              <button onClick={() => downloadClosedCsv(day, mode)}
                className="rounded border border-[#22c55e]/60 px-3 py-1.5 text-xs font-semibold text-[#22c55e] hover:bg-[#22c55e]/10">
                Download closed CSV
              </button>
            </div>
            <TradeTable rows={rowsForTable} closed mode={mode} />
          </section>;
        })}
      </div>
    </div>
  </div>;
}
