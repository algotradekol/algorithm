'use client';

import { useRef, useState } from 'react';
import SilverBacktestChart from './SilverBacktestChart';

type Candle = { time: string; open: number; high: number; low: number; close: number; volume: number; ema20: number; volume_ema20: number; partial?: boolean };
type Move = { status?: string; calculated_at?: string; candidate_sl?: number };
type Trade = {
  id: string; side: string; qty: number; entry_time: string; entry_price: number;
  exit_time?: string; exit_price?: number; exit_reason?: string; initial_sl: number;
  sl_price: number; target_price: number; net_pnl?: number; unrealized_pnl?: number;
  gross_pnl?: number; fees?: number; pnl_inr?: number; estimated_entry_margin?: number;
  signal_snapshot: { setup_time?: string; setup_close?: number; trigger_level?: number;
    silver_breakeven?: { armed: boolean; armed_at?: string };
    delta_three_candle_tsl?: { events?: unknown[] } };
};
type Result = { symbol: string; minutes: number; currency: string; start: number; end: number; candles?: Candle[]; trades: Trade[]; open_position?: Trade | null };
const format = (value?: number) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: 4 });
const timestamp = (value?: string) => value ? new Date(value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '--';
const sessionFormatter = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit' });
const session = (value: string) => sessionFormatter.format(new Date(value));
const tone = (value: number) => value < 0 ? 'text-[#ef4444]' : value > 0 ? 'text-[#22c55e]' : 'text-gray-300';

export default function DeltaBacktestReplay({ result }: { result: Result }) {
  const [selectedDate, setSelectedDate] = useState('Full range');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [overlays, setOverlays] = useState({ ema: true, setups: false, trades: true, levels: true, trailing: true });
  const chartRef = useRef<HTMLDivElement>(null);
  const candles = result.candles || [];
  const rows = [...result.trades, ...(result.open_position ? [result.open_position] : [])];
  const trades = rows.map(trade => {
    const snap = trade.signal_snapshot;
    const moves = (snap.delta_three_candle_tsl?.events || []) as Move[];
    const trailing = moves.filter(move => move.status === 'accepted' && move.calculated_at && move.candidate_sl != null).map(move => ({
      // Pair timestamps identify candle opens; a new stop is available at its close.
      time: new Date(Math.max(Date.parse(trade.entry_time), Date.parse(move.calculated_at!) + result.minutes * 60000)).toISOString(),
      new_sl: move.candidate_sl,
    }));
    if (snap.silver_breakeven?.armed && snap.silver_breakeven.armed_at) trailing.push({ time: snap.silver_breakeven.armed_at, new_sl: trade.sl_price });
    return { ...trade, trade_id: trade.id, is_open: !trade.exit_time,
      exit_time: trade.exit_time || new Date((result.end - 1) * 1000).toISOString(),
      exit_price: trade.exit_price ?? candles[candles.length - 1]?.close ?? trade.entry_price,
      net_pnl: trade.net_pnl ?? trade.unrealized_pnl,
      initial_sl_price: trade.initial_sl, final_sl_price: trade.sl_price,
      trailing_moves: trailing, active_reference_close: snap.setup_close,
      trigger_level_used: snap.trigger_level, entry_mode_label: 'Directional breakout (simulated)',
    };
  });
  const setups = rows.filter(row => row.signal_snapshot.setup_time).map(row => ({
    side: row.side, time: row.signal_snapshot.setup_time,
    setup_close: row.signal_snapshot.setup_close, trigger_level: row.signal_snapshot.trigger_level,
  }));
  const chart = { symbol: result.symbol, resolution: String(result.minutes), candles, trades, setups };
  const grouped: Record<string, Candle[]> = {};
  for (const candle of candles) {
    if (Date.parse(candle.time) + result.minutes * 60000 <= result.start * 1000) continue;
    const key = session(candle.time);
    (grouped[key] ||= []).push(candle);
  }
  const dates = Object.keys(grouped);
  const days = [{ date: 'Full range', chart }, ...dates.map(date => {
    const daily = grouped[date];
    const first = Date.parse(daily[0].time), last = Date.parse(daily[daily.length - 1].time) + result.minutes * 60000;
    return { date, chart: { ...chart, candles: daily, trades: trades.filter(t => Date.parse(t.entry_time) < last && Date.parse(t.exit_time) >= first) } };
  })];
  function focus(id: string) {
    setSelectedId(id); setSelectedDate('Full range');
    chartRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  const selected = rows.find(row => row.id === selectedId);
  return <div className="space-y-4">
    <div ref={chartRef} className="scroll-mt-4">
      <div className="mb-3 flex flex-wrap items-center gap-4 text-xs text-gray-300">
        {(['ema', 'setups', 'trades', 'levels', 'trailing'] as const).map(key => <label key={key} className="flex items-center gap-2"><input type="checkbox" checked={overlays[key]} onChange={e => setOverlays({ ...overlays, [key]: e.target.checked })} />{{ ema: 'Price EMA20', setups: 'Traded references', trades: 'Trade markers', levels: 'SL / target', trailing: 'Trailing path' }[key]}</label>)}
        <button className="ml-auto rounded border border-[#334155] px-3 py-2" onClick={() => setExpanded(!expanded)}>{expanded ? 'Compact chart' : 'Expand chart'}</button>
      </div>
      {candles.length ? <SilverBacktestChart days={days} selectedDate={selectedDate} onSelectedDateChange={setSelectedDate} selectedTradeId={selectedId} onSelectedTradeIdChange={setSelectedId} overlays={overlays} expanded={expanded} title={`${result.symbol} Replay Chart`} currency={result.currency} volumeEma /> : <p className="rounded border border-[#334155] p-4 text-sm text-gray-400">Candle chart unavailable for this result. Run the backtest after the backend update is deployed.</p>}
      <p className="mt-2 text-xs text-gray-500">Times are IST. Click a candle for OHLC and volume, or select a trade to inspect its stop and target. The final forming candle uses only minutes available at the range end.</p>
    </div>
    {selected && <div className="rounded border border-[#38bdf8]/40 bg-[#111827] p-4">
      <div className="flex flex-wrap justify-between gap-2"><h3 className="text-sm font-semibold text-[#7dd3fc]">Selected trade: {selected.side} / {selected.exit_reason || 'OPEN'}</h3><span className={`font-mono ${tone(selected.net_pnl ?? selected.unrealized_pnl ?? 0)}`}>{result.currency} {format(selected.net_pnl ?? selected.unrealized_pnl)}</span></div>
      <div className="mt-3 grid gap-3 text-xs text-gray-300 sm:grid-cols-2 lg:grid-cols-4">
        <span>Reference candle: {timestamp(selected.signal_snapshot.setup_time)}</span><span>Reference close: {format(selected.signal_snapshot.setup_close)}</span><span>Trigger: {format(selected.signal_snapshot.trigger_level)}</span><span>Lots: {format(selected.qty)}</span>
        <span>Gross: {format(selected.gross_pnl)} {result.currency}</span><span>Fees: {format(selected.fees)} {result.currency}</span><span>Net / open INR: {format(selected.pnl_inr)}</span><span>Est. margin: {format(selected.estimated_entry_margin)} {result.currency}</span>
      </div>
    </div>}
    {([['Open at range end', result.open_position ? [result.open_position] : []], ['Closed trades', [...result.trades].reverse()]] as [string, Trade[]][]).map(([label, list]) => <section key={label}>
      <h3 className="mb-2 text-sm font-semibold text-gray-200">{label} <span className="text-gray-500">({list.length})</span></h3>
      <div className="max-h-[32rem] overflow-auto rounded border border-[#1f2937]"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="sticky top-0 bg-[#111827] text-gray-400"><tr>{['Side', 'Lots', 'Entry time (IST)', 'Entry', 'Exit time (IST)', 'Exit', 'Initial SL', 'Final SL', 'Target', 'Reason', `Net / open (${result.currency})`, 'INR', 'Chart'].map(h => <th className="p-3" key={h}>{h}</th>)}</tr></thead><tbody>
        {!list.length && <tr><td colSpan={13} className="p-4 text-gray-500">No trades.</td></tr>}
        {list.map(row => <tr key={row.id} className={`border-t border-[#1f2937] text-gray-200 ${selectedId === row.id ? 'bg-[#38bdf8]/10' : 'hover:bg-[#111827]'}`}>
          <td className={`p-3 font-semibold ${row.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{row.side}</td>
          {[format(row.qty), timestamp(row.entry_time), format(row.entry_price), timestamp(row.exit_time), format(row.exit_price), format(row.initial_sl), format(row.sl_price), format(row.target_price)].map((v, i) => <td key={i} className="p-3 font-mono">{v}</td>)}
          <td className="p-3"><span className="rounded border border-[#334155] px-2 py-1 text-[#fbbf24]">{row.exit_reason || 'OPEN'}</span></td>
          <td className={`p-3 font-mono font-semibold ${tone(row.net_pnl ?? row.unrealized_pnl ?? 0)}`}>{format(row.net_pnl ?? row.unrealized_pnl)}</td><td className="p-3 font-mono">{format(row.pnl_inr)}</td>
          <td className="p-3"><button className="rounded border border-[#38bdf8]/40 px-2 py-1 text-[#7dd3fc]" onClick={() => focus(row.id)}>View chart</button></td>
        </tr>)}
      </tbody></table></div>
    </section>)}
  </div>;
}
