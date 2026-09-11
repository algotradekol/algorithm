'use client';

import { useState } from 'react';
import { api } from '../lib/api';

type Settings = { silver_breakout_points: number; sl_points: number; target_points: number; tsl_activate_points: number; silver_lots: number; exit_mode: string };
type Trade = { id: string; side: string; qty: number; entry_time: string; entry_price: number; exit_time?: string; exit_price?: number; exit_reason?: string; initial_sl: number; sl_price: number; target_price: number; estimated_entry_margin?: number; margin_inr?: number; net_pnl?: number; gross_pnl?: number; fees?: number; pnl_inr?: number; unrealized_pnl?: number; signal_snapshot: { setup_time?: string; setup_close?: number; trigger_level?: number; silver_breakeven?: { armed: boolean; activation_price: number } } };
type TriggerDiagnostic = { reference_close: number; reference_time: string; trigger: number; observed_price: number; minute: string; remaining_points: number };
type Diagnostics = {
  explanation?: string; buy_reference_updates: number; sell_reference_updates: number; buy_threshold_minutes: number; sell_threshold_minutes: number;
  warmup_buy_reference?: number; warmup_sell_reference?: number; price_low: number; price_high: number; closest_buy?: TriggerDiagnostic | null; closest_sell?: TriggerDiagnostic | null;
  eligible_events: number; already_positioned: number; cooldown_blocked: number; entries: number;
};
type Result = { symbol: string; minutes: number; path: string; start: number; end: number; currency: string; inr_rate?: number; settings: Settings; diagnostics?: Diagnostics; warnings: string[]; trades: Trade[]; open_position?: Trade; equity: { time: number; net: number; equity: number }[]; summary: { trades: number; wins: number; gross: number; fees: number; net: number; net_inr?: number; max_drawdown: number }; coverage: { minutes: number; reference_bars: number } };
const initial: Settings = { silver_breakout_points: 200, sl_points: 200, target_points: 2000, tsl_activate_points: 500, silver_lots: 1, exit_mode: 'fixed_target_sl' };
const control = 'w-full rounded border border-[#334155] bg-[#0a0e14] px-3 py-2 text-sm text-gray-100';
const button = 'rounded border border-[#334155] px-3 py-2 text-sm text-gray-200 disabled:opacity-40';
const num = (value?: number) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: 4 });
const date = (value?: string | number) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '--';
const day = (offset: number) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(Date.now() + offset * 86400000));

export default function DeltaBacktestTab() {
  const [minutes, setMinutes] = useState(15);
  const [start, setStart] = useState(() => day(-1));
  const [end, setEnd] = useState(() => day(-1));
  const [path, setPath] = useState('high_first');
  const [settings, setSettings] = useState<Settings>(initial);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function run(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setResult(null);
    try { setResult(await api.deltaBacktest({ minutes, start_date: start, end_date: end, path, settings })); }
    catch (err) { setError(err instanceof Error ? err.message : 'Backtest failed'); }
    finally { setBusy(false); }
  }
  async function loadSettings() {
    setBusy(true); setError('');
    try {
      const data = await api.deltaStatus(minutes);
      if (!data.settings) throw new Error('Saved paper settings unavailable');
      setSettings(Object.fromEntries(Object.keys(initial).map(key => [key, data.settings[key]])) as Settings);
    } catch (err) { setError(err instanceof Error ? err.message : 'Settings unavailable'); }
    finally { setBusy(false); }
  }
  return <section className="space-y-4">
    <header><h1 className="text-lg font-semibold text-gray-100">Delta Gold Backtest</h1><p className="mt-2 text-sm text-gray-400">15-minute or 1-hour EMA20 references, replayed against 1-minute candles. Runs in isolated memory, with no account orders or changes to paper settings.</p></header>
    <form onSubmit={run} className="rounded border border-[#1f2937] bg-[#111827] p-4"><fieldset disabled={busy} className="space-y-4 disabled:opacity-60">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="text-xs text-gray-400">Strategy<select className={`${control} mt-1`} value={minutes} onChange={e => setMinutes(Number(e.target.value))}><option value={15}>Delta Gold 15 min</option><option value={60}>Delta Gold 1 hr</option></select></label>
        <label className="text-xs text-gray-400">From (IST)<input className={`${control} mt-1`} type="date" required max={end} value={start} onChange={e => setStart(e.target.value)} /></label>
        <label className="text-xs text-gray-400">Through (IST, inclusive)<input className={`${control} mt-1`} type="date" required min={start} max={day(0)} value={end} onChange={e => setEnd(e.target.value)} /></label>
        <label className="text-xs text-gray-400">Assumed 1-minute path<select className={`${control} mt-1`} value={path} onChange={e => setPath(e.target.value)}><option value="high_first">Open → High → Low → Close</option><option value="low_first">Open → Low → High → Close</option></select></label>
        <label className="text-xs text-gray-400">Exit mode<select className={`${control} mt-1`} value={settings.exit_mode} onChange={e => setSettings({ ...settings, exit_mode: e.target.value })}><option value="fixed_target_sl">Fixed target + SL</option><option value="target_to_breakeven_sl">Target + breakeven SL</option></select></label>
        {([['silver_breakout_points', 'Breakout offset'], ['sl_points', 'Initial SL points'], ['target_points', 'Final target points'], ['tsl_activate_points', 'TSL activation points'], ['silver_lots', 'Lots per trade']] as const).filter(([key]) => key !== 'tsl_activate_points' || settings.exit_mode === 'target_to_breakeven_sl').map(([key, label]) => <label key={key} className="text-xs text-gray-400">{label}<input className={`${control} mt-1`} type="number" required min={key === 'silver_lots' ? 1 : .000001} step={key === 'silver_lots' ? 1 : 'any'} value={settings[key]} onChange={e => setSettings({ ...settings, [key]: Number(e.target.value) })} /></label>)}
      </div>
      <p className="text-xs text-gray-400">Maximum 31 days; today uses completed minutes only. One lot equals one exchange quantity unit. Risk distances use the quoted price, not INR. Missing candles fail the run instead of inventing prices.</p>
      <div className="flex flex-wrap gap-2"><button className={`${button} border-[#3b82f6] bg-[#3b82f6]/15`} type="submit">{busy ? 'Loading / replaying...' : 'Run backtest'}</button><button className={button} type="button" onClick={loadSettings}>Load this strategy&apos;s paper settings</button></div>
    </fieldset></form>
    {error && <p role="alert" className="rounded border border-[#ef4444]/40 p-3 text-sm text-[#f87171]">{error}</p>}
    {result && <>
      <div className="flex flex-wrap items-center justify-between gap-3"><div className="text-sm text-gray-300">{result.symbol} | {result.minutes}m | {date(result.start)} to {date(result.end)} IST<br /><span className="text-xs text-gray-500">{num(result.coverage.minutes)} execution candles | {num(result.coverage.reference_bars)} reference bars including warmup | {result.path === 'high_first' ? 'O-H-L-C' : 'O-L-H-C'}</span></div><button className={button} onClick={() => download(result)}>Download full CSV</button></div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[['Closed trades / wins', `${result.summary.trades} / ${result.summary.wins}`], [`Realized net (${result.currency})`, num(result.summary.net)], ['Realized net (INR)', num(result.summary.net_inr)], [`Max equity drawdown (${result.currency})`, num(result.summary.max_drawdown)]].map(([label, value]) => <div key={label} className="rounded border border-[#1f2937] bg-[#111827] p-3"><p className="text-xs text-gray-400">{label}</p><p className="mt-2 font-mono text-lg text-gray-100">{value}</p></div>)}</div>
      <DiagnosticsPanel diagnostics={result.diagnostics} settings={result.settings} currency={result.currency} />
      <EquityChart points={result.equity} currency={result.currency} />
      <div className="rounded border border-[#f59e0b]/30 p-3 text-xs text-[#fbbf24]">{result.warnings.map(w => <p className="mb-1" key={w}>{w}</p>)}</div>
      <h2 className="text-sm text-gray-300">OPEN AT RANGE END</h2><Results rows={result.open_position ? [result.open_position] : []} />
      <h2 className="text-sm text-gray-300">CLOSED TRADES</h2><Results rows={[...result.trades].reverse()} />
    </>}
  </section>;
}

function DiagnosticsPanel({ diagnostics, settings, currency }: { diagnostics?: Diagnostics; settings: Settings; currency: string }) {
  if (!diagnostics) return null;
  const cards = [
    ['BUY references', num(diagnostics.buy_reference_updates), 'Completed reference bars accepted'],
    ['SELL references', num(diagnostics.sell_reference_updates), 'Completed reference bars accepted'],
    ['BUY threshold minutes', num(diagnostics.buy_threshold_minutes), 'Minutes where high reached reference + offset'],
    ['SELL threshold minutes', num(diagnostics.sell_threshold_minutes), 'Minutes where low reached reference - offset'],
  ];
  return <div className="rounded border border-[#334155] bg-[#0f172a] p-3">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 className="text-sm font-semibold text-gray-100">Entry diagnostics</h2>
        <p className="mt-1 text-xs text-gray-400">{diagnostics.explanation || 'Diagnostics generated for this replay.'}</p>
      </div>
      <div className="rounded border border-[#475569] px-3 py-2 text-xs text-gray-300">
        Offset used: <span className="font-mono text-white">{num(settings.silver_breakout_points)}</span> {currency} price points
      </div>
    </div>
    <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">{cards.map(([label, value, helper]) => <div key={label} className="rounded border border-[#1f2937] bg-[#111827] p-3"><p className="text-xs text-gray-400">{label}</p><p className="mt-1 font-mono text-lg text-gray-100">{value}</p><p className="mt-1 text-[11px] text-gray-500">{helper}</p></div>)}</div>
    <div className="mt-3 grid gap-3 xl:grid-cols-2">
      <TriggerGap title="Closest BUY trigger" diagnostic={diagnostics.closest_buy} side="BUY" currency={currency} />
      <TriggerGap title="Closest SELL trigger" diagnostic={diagnostics.closest_sell} side="SELL" currency={currency} />
    </div>
    <p className="mt-3 text-xs text-gray-500">Replay price range: {num(diagnostics.price_low)} to {num(diagnostics.price_high)}. Eligible entry events: {num(diagnostics.eligible_events)}, simulated entries: {num(diagnostics.entries)}, cooldown blocks: {num(diagnostics.cooldown_blocked)}.</p>
  </div>;
}

function TriggerGap({ title, diagnostic, side, currency }: { title: string; diagnostic?: TriggerDiagnostic | null; side: string; currency: string }) {
  if (!diagnostic) return <div className="rounded border border-[#1f2937] bg-[#111827] p-3 text-xs text-gray-500">{title}: no active reference found.</div>;
  const hit = diagnostic.remaining_points <= 0;
  return <div className={`rounded border p-3 ${hit ? 'border-[#22c55e]/40 bg-[#052e1a]/30' : 'border-[#f59e0b]/40 bg-[#1f1604]/30'}`}>
    <p className={`text-xs font-semibold ${hit ? 'text-[#22c55e]' : 'text-[#fbbf24]'}`}>{title}</p>
    <div className="mt-2 grid gap-2 text-xs text-gray-300 sm:grid-cols-2">
      <span>Reference <b className="font-mono text-white">{num(diagnostic.reference_close)}</b></span>
      <span>Reference time <b className="font-mono text-white">{date(diagnostic.reference_time)}</b></span>
      <span>Trigger <b className="font-mono text-white">{num(diagnostic.trigger)}</b></span>
      <span>{side === 'BUY' ? 'Highest high seen' : 'Lowest low seen'} <b className="font-mono text-white">{num(diagnostic.observed_price)}</b></span>
      <span>Closest minute <b className="font-mono text-white">{date(diagnostic.minute)}</b></span>
      <span>Remaining <b className="font-mono text-white">{hit ? '0' : num(diagnostic.remaining_points)}</b> {currency} points</span>
    </div>
  </div>;
}

function EquityChart({ points, currency }: { points: Result['equity']; currency: string }) {
  if (!points.length) return null;
  const values = points.map(p => p.equity);
  const low = Math.min(0, ...values), high = Math.max(0, ...values), span = high - low || 1;
  const line = points.map((p, i) => `${50 + i / Math.max(1, points.length - 1) * 900},${200 - (p.equity - low) / span * 170}`).join(' ');
  return <div className="rounded border border-[#1f2937] bg-[#111827] p-3"><p className="text-sm text-gray-300">Simulated equity ({currency}, includes open P&amp;L)</p><svg className="mt-2 w-full" viewBox="0 0 1000 230" role="img" aria-label={`Equity range ${num(low)} to ${num(high)} ${currency}`}><line x1="50" x2="950" y1={200 - (0 - low) / span * 170} y2={200 - (0 - low) / span * 170} stroke="#475569" strokeDasharray="4 4" /><polyline points={line} fill="none" stroke="#38bdf8" strokeWidth="2" /><text x="50" y="20" fill="#94a3b8" fontSize="12">{num(high)}</text><text x="50" y="220" fill="#94a3b8" fontSize="12">{num(low)}</text></svg></div>;
}

function Results({ rows }: { rows: Trade[] }) {
  const headers = ['Side', 'Lots', 'Reference time (IST)', 'Ref close', 'Trigger', 'Entry time (simulated IST)', 'Entry', 'Exit time (simulated IST)', 'Exit', 'Reason', 'Initial SL', 'Final SL', 'Target', 'Breakeven armed', 'Est. margin', 'Net / open P&L', 'INR'];
  return <div className="max-h-[32rem] overflow-auto rounded border border-[#1f2937]"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="sticky top-0 bg-[#111827]"><tr>{headers.map(h => <th key={h} className="p-3 text-gray-400">{h}</th>)}</tr></thead><tbody>{!rows.length && <tr><td colSpan={headers.length} className="p-4 text-gray-500">No trades.</td></tr>}{rows.map(r => <tr key={r.id} className="border-t border-[#1f2937] text-gray-200"><td className={`p-3 ${r.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{r.side}</td>{[num(r.qty), date(r.signal_snapshot.setup_time), num(r.signal_snapshot.setup_close), num(r.signal_snapshot.trigger_level), date(r.entry_time), num(r.entry_price), date(r.exit_time), num(r.exit_price), r.exit_reason || 'OPEN', num(r.initial_sl), num(r.sl_price), num(r.target_price), r.signal_snapshot.silver_breakeven?.armed ? 'Yes' : 'No', num(r.estimated_entry_margin), num(r.net_pnl ?? r.unrealized_pnl), num(r.pnl_inr)].map((v, i) => <td className="p-3 font-mono" key={i}>{v}</td>)}</tr>)}</tbody></table></div>;
}

function download(result: Result) {
  const columns = ['id', 'side', 'lots', 'entry_time', 'entry_price', 'exit_time', 'exit_price', 'reason', 'gross', 'fees', 'net', 'open_pnl', 'pnl_inr', 'initial_sl', 'final_sl', 'target', 'margin', 'margin_inr', 'signal_snapshot', 'run_settings', 'diagnostics', 'path', 'timeframe', 'symbol', 'start', 'end', 'warnings'];
  const cell = (v: unknown) => { let text = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v); if (typeof v === 'string' && /^[=+@-]/.test(text)) text = "'" + text; return `"${text.replace(/"/g, '""')}"`; };
  const tradeRows = [...result.trades, ...(result.open_position ? [result.open_position] : [])];
  const sourceRows = tradeRows.length ? tradeRows : [{ id: 'NO_TRADES' } as Trade];
  const lines = sourceRows.map(r => [r.id, r.side, r.qty, r.entry_time, r.entry_price, r.exit_time, r.exit_price, r.exit_reason || (r.id === 'NO_TRADES' ? 'NO_TRADES' : 'OPEN'), r.gross_pnl, r.fees, r.net_pnl, r.unrealized_pnl, r.pnl_inr, r.initial_sl, r.sl_price, r.target_price, r.estimated_entry_margin, r.margin_inr, r.signal_snapshot, result.settings, result.diagnostics, result.path, result.minutes, result.symbol, result.start, result.end, result.warnings].map(cell).join(','));
  const url = URL.createObjectURL(new Blob([[columns.join(','), ...lines].join('\r\n')], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = `delta-${result.minutes}m-backtest-${result.start}.csv`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
