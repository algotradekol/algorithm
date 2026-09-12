'use client';

import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';

type Settings = {
  scan_enabled: boolean; trading_enabled: boolean; silver_breakout_points: number;
  sl_points: number; target_points: number; tsl_activate_points: number; tsl_buffer_points: number; silver_lots: number;
  exit_mode: 'fixed_target_sl' | 'target_to_breakeven_sl' | 'three_candle_tsl'; manual_exit_reentry_enabled: boolean;
  post_exit_cooldown_minutes: number;
  strategy_version: string;
};
type Trade = {
  id: string; symbol: string; side: string; qty: number; entry_time: string; entry_price: number;
  sl_price: number; initial_sl: number; target_price: number; trailing_sl_active: boolean;
  exit_time?: string; exit_price?: number; exit_reason?: string; gross_pnl?: number; fees?: number;
  net_pnl?: number; unrealized_pnl?: number; estimated_entry_margin?: number; margin_inr?: number; pnl_inr?: number;
  signal_snapshot: { setup_time?: string; setup_close?: number; trigger_level?: number; entry_candle_open?: number; silver_breakeven?: { activation_price: number; armed: boolean }; delta_three_candle_tsl?: { events?: any[]; evaluations?: any[] } };
};
type Status = {
  settings?: Settings; symbol?: string; exchange?: string; error?: string; history_error?: string;
  ltp?: number; last_tick_at?: number; last_bar_at?: number; stale: boolean; source?: string;
  ws_connected: boolean; ws_error?: string; proxy_configured: boolean; credentials_configured: boolean;
  position?: Trade; quote_currency?: string; settlement_currency?: string; contract_value?: number; contract_unit?: string;
  ema20?: number; volume_ema20?: number; current_candle_open?: number; buy_reference?: number; sell_reference?: number; inr_rate?: number;
  summary?: { gross_pnl: number; fees: number; closed_count: number; buy_count: number; sell_count: number };
  cooldown?: { active: boolean; until?: number; remaining_seconds: number; reason?: string };
  references?: { side: string; time: string; open: number; high: number; low: number; close: number; volume: number; ema20: number; volume_ema20: number }[];
};

const number = (value?: number | null) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: 6 });
const date = (value?: string | number) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '--';
const duration = (value?: number) => {
  const seconds = Math.max(0, Math.ceil(value || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours ? `${hours}h ` : ''}${minutes}m ${seconds % 60}s`;
};
const button = 'rounded border border-[#334155] px-3 py-2 text-sm text-gray-200 hover:border-[#60a5fa] disabled:opacity-40';

export default function DeltaTab({ minutes }: { minutes: number }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [editing, setEditing] = useState<Trade | null>(null);
  const [editError, setEditError] = useState('');
  const [csvBusy, setCsvBusy] = useState<'open' | 'closed' | null>(null);
  const [restPreset, setRestPreset] = useState('5');
  const [customRestMinutes, setCustomRestMinutes] = useState(45);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await api.deltaStatus(minutes);
        if (cancelled) return;
        setStatus(next);
        if (next.settings) {
          const rows = await api.deltaTrades(minutes, offset);
          if (!cancelled) setTrades(rows.trades);
        }
        if (!cancelled) setError('');
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Delta status unavailable');
      } finally {
        if (!cancelled) timer = setTimeout(poll, 3000);
      }
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [minutes, offset, reload]);

  async function action(work: () => Promise<unknown>, success: string) {
    setBusy(true); setNotice('');
    try { await work(); setNotice(success); setReload(value => value + 1); }
    catch (err) { setNotice(err instanceof Error ? err.message : 'Action failed'); }
    finally { setBusy(false); }
  }

  const settings = status?.settings;
  const currency = status?.quote_currency || 'quote currency';
  const pnl = status?.summary;
  const cooldown = status?.cooldown;
  const selectedRestMinutes = restPreset === 'custom' ? customRestMinutes : Number(restPreset);
  async function download(kind: 'open' | 'closed') {
    setCsvBusy(kind); setNotice('');
    try {
      const result = await api.deltaExport(minutes, kind);
      const url = URL.createObjectURL(new Blob([result.csv], { type: 'text/csv;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = result.filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(`Exported ${result.count} ${kind} paper trades`);
    } catch (err) { setNotice(err instanceof Error ? err.message : 'CSV export failed'); }
    finally { setCsvBusy(null); }
  }
  async function saveProtection(sl: number, target: number) {
    if (!editing) return;
    setBusy(true); setEditError('');
    try {
      await api.deltaProtection(minutes, { position_id: editing.id, sl_price: sl, target_price: target, expected_sl: editing.sl_price, expected_target: editing.target_price });
      setEditing(null); setNotice('Paper SL / target saved'); setReload(v => v + 1);
    } catch (err) { setEditError(err instanceof Error ? err.message : 'Protection was not saved'); }
    finally { setBusy(false); }
  }
  function exit(row: Trade) {
    if (window.confirm(`Exit this ${row.side} paper position now at the latest Delta price?`)) {
      void action(() => api.deltaClose(minutes, row.id), 'Paper exit confirmed');
    }
  }
  return <div className="space-y-4">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 className="text-lg font-semibold text-gray-100">Delta Gold {minutes === 60 ? '1 hr' : minutes === 240 ? '4 hr' : `${minutes} min`} <span className="ml-2 rounded bg-[#3b82f6]/20 px-2 py-1 text-xs text-[#93c5fd]">PAPER ONLY</span></h1>
        <p className="mt-2 text-sm text-gray-400">{status?.symbol || 'Gold symbol not configured'} | 24/7 | Price EMA20 + volume EMA20 | No daily square-off</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {settings && <>
          <button className={button} disabled={busy} onClick={() => action(() => api.deltaSettings(minutes, { scan_enabled: !settings.scan_enabled }), 'Scan setting saved')}>Scan: {settings.scan_enabled ? 'ON' : 'OFF'}</button>
          <button className={button} disabled={busy} onClick={() => action(() => api.deltaSettings(minutes, { trading_enabled: !settings.trading_enabled }), 'Paper trading setting saved')}>Trading: {settings.trading_enabled ? 'ON' : 'OFF'}</button>
          <button className={button} onClick={() => setDraft({ ...settings })}>Settings</button>
          <select aria-label="Entry rest duration" className={`${button} bg-[#0a0e14]`} value={restPreset} disabled={busy} onChange={e => setRestPreset(e.target.value)}>
            <option value="5">5 min rest</option><option value="15">15 min rest</option><option value="30">30 min rest</option>
            <option value="60">1 hr rest</option><option value="240">4 hr rest</option><option value="720">12 hr rest</option><option value="custom">Custom minutes</option>
          </select>
          {restPreset === 'custom' && <input aria-label="Custom entry rest minutes" className={`${button} w-28 bg-[#0a0e14]`} type="number" min={1} max={10080} step={1} value={customRestMinutes} disabled={busy} onChange={e => setCustomRestMinutes(Number(e.target.value))} />}
          <button className={`${button} border-[#f59e0b]/70 text-[#fbbf24]`} disabled={busy || !Number.isInteger(selectedRestMinutes) || selectedRestMinutes < 1 || selectedRestMinutes > 10080} onClick={() => action(() => api.deltaPause(minutes, selectedRestMinutes), `New entries paused for ${selectedRestMinutes} minutes`)}>Pause entries</button>
          <button className={`${button} border-[#22c55e]/70 text-[#22c55e]`} disabled={busy || !cooldown?.active} onClick={() => action(() => api.deltaResume(minutes), 'Entry rest cleared. The next qualifying crossing may trade.')}>Resume</button>
        </>}
        <button className={button} disabled={busy || !status?.credentials_configured} onClick={() => action(() => api.deltaCheckConnection(), 'Delta account verified. Execution remains paper only.')}>Verify API connection</button>
      </div>
    </header>
    {(error || notice || status?.error || status?.history_error) && <div role="status" className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 p-3 text-sm text-[#fbbf24]">{error || notice || status?.error || status?.history_error}</div>}
    {cooldown?.active && <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-[#f59e0b]/50 bg-[#f59e0b]/10 p-3">
      <div><div className="text-sm font-semibold text-[#fbbf24]">Entry rest active: {duration(cooldown.remaining_seconds)} remaining</div><p className="mt-1 text-xs text-gray-400">Started after {cooldown.reason?.replaceAll('_', ' ')}. New entries resume at {date(cooldown.until)} IST. Scanning, references, open-position exits, and EMA calculations continue.</p></div>
    </div>}
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
      <Card label="Market data" value={status?.stale ? 'Waiting for fresh trade' : `${status?.source || '--'} active`} detail={`WS ${status?.ws_connected ? 'connected' : 'disconnected'} | ${status?.proxy_configured ? 'VM proxy' : 'Direct connection'}`} />
      <Card label={`LTP (${currency})`} value={number(status?.ltp)} detail={`Last trade: ${date(status?.last_tick_at)} IST`} />
      <Card label="EMA20" value={number(status?.ema20)} detail={`Last completed candle: ${date(status?.last_bar_at)} IST`} />
      <Card label="Volume EMA20" value={number(status?.volume_ema20)} detail="Completed strategy candles" />
      <Card label="Active candle open" value={number(status?.current_candle_open)} detail="Must begin on the valid side of the trigger" />
      <Card label="Lots per trade" value={number(settings?.silver_lots)} detail={`1 lot = ${number(status?.contract_value)} ${status?.contract_unit || ''} | Same exchange quantity, renamed only`} />
    </div>
    {status?.ws_error && <p className="text-xs text-gray-400">{status.ws_error}</p>}
    <div className="grid gap-3 md:grid-cols-2">
      {(['BUY', 'SELL'] as const).map(side => {
        const ref = side === 'BUY' ? status?.buy_reference : status?.sell_reference;
        const time = status?.references?.find(row => row.side === side)?.time;
        const trigger = ref != null && settings ? ref + (side === 'BUY' ? 1 : -1) * settings.silver_breakout_points : undefined;
        return <div key={side} className={`rounded border p-4 ${side === 'BUY' ? 'border-[#22c55e]/40 bg-[#22c55e]/5' : 'border-[#ef4444]/40 bg-[#ef4444]/5'}`}>
          <div className={side === 'BUY' ? 'text-sm text-[#22c55e]' : 'text-sm text-[#ef4444]'}>{side} reference: {side === 'BUY' ? 'green close > EMA20' : 'red close < EMA20'} + volume &gt; volume EMA20</div>
          <div className="mt-2 font-mono text-gray-100">Close {number(ref)} | Trigger {number(trigger)}</div>
          <p className="mt-2 text-xs text-gray-400">{date(time)} IST | Volume {number(status?.references?.find(row => row.side === side)?.volume)} | Volume EMA20 {number(status?.references?.find(row => row.side === side)?.volume_ema20)}</p>
        </div>;
      })}
    </div>
    <div className="grid gap-3 sm:grid-cols-3">
      <Card label="Closed trades (all time)" value={number(pnl?.closed_count)} detail={`${number(pnl?.buy_count)} BUY / ${number(pnl?.sell_count)} SELL entries, including open trades`} />
      <Card label={`Realized gross (${currency})`} value={number(pnl?.gross_pnl)} detail="Closed paper trades only" />
      <Card label={`Estimated net (${currency})`} value={pnl ? number(pnl.gross_pnl - pnl.fees) : '--'} detail="Product taker fees deducted; funding, taxes and slippage excluded" />
    </div>
    {draft && <form className="panel space-y-4 p-4" onSubmit={event => {
      event.preventDefault();
      void action(async () => { await api.deltaSettings(minutes, draft); setDraft(null); }, 'Delta settings saved');
    }}>
      <h2 className="font-semibold text-gray-100">Paper risk settings</h2>
      <p className="text-xs text-gray-400">All distances are in the quoted gold price, not rupees. Existing positions retain their entry-time protection.</p>
      <label className="block text-sm text-gray-300">Exit mode
        <select className="mt-1 block w-full rounded border border-[#334155] bg-[#0a0e14] p-2" value={draft.exit_mode} onChange={e => setDraft({ ...draft, exit_mode: e.target.value as Settings['exit_mode'] })}>
          <option value="fixed_target_sl">Fixed Target + Fixed Stop Loss</option>
          <option value="target_to_breakeven_sl">Target + Breakeven Stop Loss</option>
          <option value="three_candle_tsl">Three-Candle TSL</option>
        </select>
      </label>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {([
          ['silver_breakout_points', 'Breakout offset'], ['sl_points', 'Initial stop loss'],
          ['target_points', 'Final target'], ['tsl_activate_points', 'TSL activates at'], ['tsl_buffer_points', 'TSL buffer points'], ['silver_lots', 'Lots per trade'],
        ] as const).filter(([key]) => (key !== 'tsl_activate_points' || draft.exit_mode === 'target_to_breakeven_sl') && (key !== 'tsl_buffer_points' || draft.exit_mode === 'three_candle_tsl')).map(([key, label]) => <label key={key} className="text-sm text-gray-300">{label}
          <input className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" type="number" required min={key === 'silver_lots' ? 1 : 0.000001} step={key === 'silver_lots' ? 1 : 'any'} value={draft[key]} onChange={e => setDraft({ ...draft, [key]: Number(e.target.value) })} />
        </label>)}
      </div>
      <label className="flex items-center gap-2 text-sm text-gray-300"><input type="checkbox" checked={draft.manual_exit_reentry_enabled} onChange={e => setDraft({ ...draft, manual_exit_reentry_enabled: e.target.checked })} /> Allow re-entry after manual exit when the signal qualifies</label>
      <label className="block text-sm text-gray-300">Rest after manual, stop, or target exit
        <input className="mt-1 block w-full rounded border border-[#334155] bg-[#0a0e14] p-2" type="number" required min={1} max={10080} step={1} value={draft.post_exit_cooldown_minutes} onChange={e => setDraft({ ...draft, post_exit_cooldown_minutes: Number(e.target.value) })} />
        <span className="mt-2 flex flex-wrap gap-2">{[[5, '5m'], [15, '15m'], [30, '30m'], [60, '1h'], [240, '4h'], [720, '12h']].map(([value, label]) => <button key={value} type="button" className="rounded border border-[#334155] px-2 py-1 text-xs text-[#93c5fd]" onClick={() => setDraft({ ...draft, post_exit_cooldown_minutes: Number(value) })}>{label}</button>)}</span>
        <span className="mt-1 block text-xs text-gray-500">Applies to manual exits, SL, trailing SL, and targets. Reversals bypass this rest.</span>
      </label>
      <div className="flex gap-2"><button className={button} disabled={busy}>Save</button><button className={button} type="button" onClick={() => setDraft(null)}>Cancel</button></div>
    </form>}
    <p className="text-xs text-gray-400">Paper margin is an entry estimate using the product base initial-margin percentage, excluding size tiers, fees and account leverage. Older records without captured margin show --. {status?.inr_rate ? `INR uses Delta India's fixed rate: 1 USD = Rs ${status.inr_rate}.` : 'INR conversion unavailable for this currency.'}</p>
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold text-gray-300">OPEN PAPER POSITION</h2><button className={`${button} border-[#22c55e]/60 text-[#22c55e]`} disabled={csvBusy !== null || !settings} onClick={() => download('open')}>{csvBusy === 'open' ? 'Exporting...' : 'Download open CSV'}</button></div>
      <TradeTable rows={status?.position ? [status.position] : []} disabled={busy || !status || status.stale || !!error} onEdit={row => { setEditing({ ...row }); setEditError(''); }} onExit={exit} />
    </section>
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold text-gray-300">CLOSED PAPER TRADES</h2><button className={`${button} border-[#22c55e]/60 text-[#22c55e]`} disabled={csvBusy !== null || !settings} onClick={() => download('closed')}>{csvBusy === 'closed' ? 'Exporting all trades...' : 'Download closed CSV'}</button></div>
      <TradeTable rows={trades} closed />
      <div className="mt-2 flex justify-end gap-2"><button className={button} disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous</button><button className={button} disabled={trades.length < 100} onClick={() => setOffset(value => value + 100)}>Next</button></div>
    </section>
    <details className="panel p-3"><summary className="cursor-pointer text-sm text-[#93c5fd]">Reference history</summary>
      <div className="mt-3 max-h-80 overflow-auto"><table className="w-full text-left text-xs"><thead><tr>{['Side', 'Candle time (IST)', 'Open', 'High', 'Low', 'Close', 'EMA20', 'Volume', 'Volume EMA20'].map(label => <th key={label} className="p-2 text-gray-400">{label}</th>)}</tr></thead><tbody>{status?.references?.map(row => <tr key={`${row.side}-${row.time}`} className="border-t border-[#1f2937] text-gray-200"><td className="p-2">{row.side}</td><td className="p-2">{date(row.time)}</td>{[row.open, row.high, row.low, row.close, row.ema20, row.volume, row.volume_ema20].map((value, index) => <td key={index} className="p-2 font-mono">{number(value)}</td>)}</tr>)}</tbody></table></div>
    </details>
    {editing && <EditProtection key={editing.id} row={editing} ltp={status?.ltp} busy={busy} disabled={!status || status.stale || !!error || status.position?.id !== editing.id} error={editError} onClose={() => { if (!busy) setEditing(null); }} onSave={saveProtection} />}
  </div>;
}

function Card({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="rounded border border-[#1f2937] bg-[#111827] p-3"><div className="text-xs uppercase tracking-wide text-gray-400">{label}</div><div className="mt-2 font-mono text-lg text-gray-100">{value}</div><p className="mt-2 text-xs text-gray-500">{detail}</p></div>;
}

function TradeTable({ rows, closed = false, disabled, onEdit, onExit }: { rows: Trade[]; closed?: boolean; disabled?: boolean; onEdit?: (row: Trade) => void; onExit?: (row: Trade) => void }) {
  const headers = ['Side', 'Lots', 'Entry time (IST)', 'Entry', 'Reference time (IST)', 'Reference', 'Initial SL', 'Current SL', 'Target', ...(!closed ? ['Edit'] : []), 'TSL', 'Est. entry margin (quote)', 'Est. entry margin (INR)', ...(closed ? ['Exit time (IST)', 'Exit', 'Reason', 'Gross', 'Fees', 'Net', 'Net (INR)'] : ['Unrealized P&L', 'Unrealized (INR)', 'Exit'])];
  return <div className="max-h-[32rem] overflow-auto rounded border border-[#1f2937]"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="sticky top-0 bg-[#111827]"><tr>{headers.map(label => <th key={label} className="px-3 py-3 text-gray-400">{label}</th>)}</tr></thead><tbody>
    {!rows.length && <tr><td colSpan={headers.length} className="p-4 text-gray-500">No {closed ? 'closed trades' : 'open position'}.</td></tr>}
    {rows.map(row => <tr key={row.id} className="border-t border-[#1f2937] text-gray-200">
      <td className={`p-3 ${row.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{row.side}</td>
      {[number(row.qty), date(row.entry_time), number(row.entry_price), date(row.signal_snapshot.setup_time), number(row.signal_snapshot.setup_close), number(row.initial_sl), number(row.sl_price), number(row.target_price), ...(!closed ? [<button key="edit" className="min-h-9 rounded border border-[#3b82f6]/70 px-2.5 py-1.5 text-xs font-semibold text-[#3b82f6] disabled:opacity-40" disabled={disabled} onClick={() => onEdit?.(row)}>Edit</button>] : []), <TslAudit key="tsl" row={row} />, number(row.estimated_entry_margin), number(row.margin_inr), ...(closed ? [date(row.exit_time), number(row.exit_price), row.exit_reason, number(row.gross_pnl), number(row.fees), number(row.net_pnl), number(row.pnl_inr)] : [number(row.unrealized_pnl), number(row.pnl_inr), <button key="exit" className="min-h-9 rounded border border-[#ef4444]/70 px-2.5 py-1.5 text-xs font-semibold text-[#ef4444] disabled:opacity-40" disabled={disabled} onClick={() => onExit?.(row)}>Exit</button>])].map((value, index) => <td key={index} className="p-3 font-mono">{value}</td>)}
    </tr>)}
  </tbody></table></div>;
}

function TslAudit({ row }: { row: Trade }) {
  const three = row.signal_snapshot.delta_three_candle_tsl;
  if (three) {
    const events = three.events || [];
    const latest = events[events.length - 1];
    return <details><summary className="cursor-pointer text-[#a78bfa]">Three-candle {events.length ? `${events.length} move(s)` : 'waiting'}</summary>{latest && <div className="mt-2 min-w-80 space-y-1 text-[11px] text-gray-400"><div>Candidate {number(latest.candidate_sl)} from {number(latest.reference_price)} with buffer {number(latest.buffer_points)}</div>{latest.candles?.map((candle: any) => <div key={candle.time}>{date(candle.time)} | O {number(candle.open)} H {number(candle.high)} L {number(candle.low)} C {number(candle.close)}</div>)}</div>}</details>;
  }
  if (row.trailing_sl_active) return <>Breakeven armed</>;
  if (row.signal_snapshot.silver_breakeven) return <>Arms at {number(row.signal_snapshot.silver_breakeven.activation_price)}</>;
  return <>Fixed</>;
}

function EditProtection({ row, ltp, busy, disabled, error, onClose, onSave }: { row: Trade; ltp?: number; busy: boolean; disabled: boolean; error: string; onClose: () => void; onSave: (sl: number, target: number) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [sl, setSl] = useState(String(row.sl_price));
  const [target, setTarget] = useState(String(row.target_price));
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} onCancel={e => { e.preventDefault(); onClose(); }} aria-labelledby="delta-protection-title" className="w-[calc(100%_-_2rem)] max-w-md rounded border border-[#1f2937] bg-[#0d1117] p-4 text-gray-100 backdrop:bg-black/70">
    <form onSubmit={e => { e.preventDefault(); onSave(Number(sl), Number(target)); }}>
      <div className="flex items-start justify-between gap-3"><div><h3 id="delta-protection-title" className="font-semibold">Edit SL / Target</h3><p className="mt-1 text-xs text-gray-400">{row.symbol} | {row.side} | Entry {number(row.entry_price)} | LTP {number(ltp)}</p></div><button type="button" aria-label="Close editor" disabled={busy} onClick={onClose}>×</button></div>
      <p className="mt-3 rounded border border-[#3b82f6]/40 p-2 text-xs text-[#93c5fd]">Paper position only. Enter absolute price levels, not distances. Settings and other positions are unchanged. TSL activation stays at its captured price and will not loosen a tighter manual stop.</p>
      <div className="mt-4 grid grid-cols-2 gap-3">{[['Stop loss', sl, setSl], ['Target', target, setTarget]].map(([label, value, setter]) => <label key={label as string} className="text-sm text-gray-300">{label as string}<input autoFocus={label === 'Stop loss'} type="number" min="0.000001" step="any" required value={value as string} disabled={busy} onChange={e => (setter as (value: string) => void)(e.target.value)} className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" /></label>)}</div>
      {(error || disabled) && <p role="alert" className="mt-3 text-sm text-[#f87171]">{error || 'Fresh price or matching open position unavailable. Reload before editing.'}</p>}
      <div className="mt-4 flex justify-end gap-2"><button className={button} type="button" disabled={busy} onClick={onClose}>Cancel</button><button className={`${button} border-[#3b82f6] bg-[#3b82f6]/20`} disabled={busy || disabled}>{busy ? 'Saving...' : 'Save'}</button></div>
    </form>
  </dialog>;
}
