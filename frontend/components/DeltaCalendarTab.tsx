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
    {selectedDay && <div className="panel overflow-x-auto">
      <div className="flex items-center justify-between border-b border-[#1f2937] px-3 py-2">
        <h3 className="text-sm font-semibold text-gray-100">{selectedDay.date} — {selectedDay.trades.length} trade{selectedDay.trades.length === 1 ? '' : 's'}</h3>
        <button onClick={() => setSelectedDate(null)} className="text-xs text-gray-500 hover:text-gray-300">close</button>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-[#1f2937] text-left text-[11px] uppercase tracking-wide text-gray-500">
            <th className="px-3 py-2">TF</th>
            <th className="px-3 py-2">Side</th>
            <th className="px-3 py-2">Entry (IST)</th>
            <th className="px-3 py-2">Exit (IST)</th>
            <th className="px-3 py-2">Exit reason</th>
            <th className="px-3 py-2 text-right">PnL (INR)</th>
          </tr>
        </thead>
        <tbody>
          {selectedDay.trades.map((trade, index) => <tr key={index} className="border-b border-[#111827]/60">
            <td className="px-3 py-2 text-gray-300">{trade.minutes}m</td>
            <td className={`px-3 py-2 font-semibold ${trade.side === 'BUY' ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>{trade.side}</td>
            <td className="px-3 py-2 text-gray-300">{istTime(trade.entry_time)}</td>
            <td className="px-3 py-2 text-gray-300">{istTime(trade.exit_time)}</td>
            <td className="px-3 py-2 text-gray-500">{trade.exit_reason || '--'}</td>
            <td className={`px-3 py-2 text-right font-semibold ${(trade.pnl_inr ?? 0) >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>{trade.pnl_inr != null ? `${trade.pnl_inr >= 0 ? '+' : ''}₹${INR.format(trade.pnl_inr)}` : '--'}</td>
          </tr>)}
        </tbody>
      </table>
    </div>}
  </div>;
}
