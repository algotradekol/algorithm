'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';

type Settings = {
  scan_enabled: boolean; trading_enabled: boolean; silver_breakout_points: number;
  sl_points: number; target_points: number; tsl_activate_points: number; silver_lots: number;
  exit_mode: 'fixed_target_sl' | 'target_to_breakeven_sl'; manual_exit_reentry_enabled: boolean;
};
type Trade = {
  id: string; symbol: string; side: string; qty: number; entry_time: string; entry_price: number;
  sl_price: number; initial_sl: number; target_price: number; trailing_sl_active: boolean;
  exit_time?: string; exit_price?: number; exit_reason?: string; gross_pnl?: number; fees?: number;
  net_pnl?: number; unrealized_pnl?: number;
  signal_snapshot: { setup_time?: string; setup_close?: number; trigger_level?: number; silver_breakeven?: { activation_price: number; armed: boolean } };
};
type Status = {
  settings?: Settings; symbol?: string; exchange?: string; error?: string; history_error?: string;
  ltp?: number; last_tick_at?: number; last_bar_at?: number; stale: boolean; source?: string;
  ws_connected: boolean; ws_error?: string; proxy_configured: boolean; credentials_configured: boolean;
  position?: Trade; quote_currency?: string; settlement_currency?: string; contract_value?: number; contract_unit?: string;
  ema20?: number; buy_reference?: number; sell_reference?: number;
  summary?: { gross_pnl: number; fees: number; closed_count: number; buy_count: number; sell_count: number };
  references?: { side: string; time: string; close: number; ema20: number }[];
};

const number = (value?: number | null) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: 6 });
const date = (value?: string | number) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '--';
const button = 'rounded border border-[#334155] px-3 py-2 text-sm text-gray-200 hover:border-[#60a5fa] disabled:opacity-40';

export default function DeltaTab({ minutes }: { minutes: 15 | 60 }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);

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
  return <div className="space-y-4">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 className="text-lg font-semibold text-gray-100">Delta Gold {minutes === 15 ? '15 min' : '1 hr'} <span className="ml-2 rounded bg-[#3b82f6]/20 px-2 py-1 text-xs text-[#93c5fd]">PAPER ONLY</span></h1>
        <p className="mt-2 text-sm text-gray-400">{status?.symbol || 'Gold symbol not configured'} | 24/7 | EMA20 reference breakout | No daily square-off</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {settings && <>
          <button className={button} disabled={busy} onClick={() => action(() => api.deltaSettings(minutes, { scan_enabled: !settings.scan_enabled }), 'Scan setting saved')}>Scan: {settings.scan_enabled ? 'ON' : 'OFF'}</button>
          <button className={button} disabled={busy} onClick={() => action(() => api.deltaSettings(minutes, { trading_enabled: !settings.trading_enabled }), 'Paper trading setting saved')}>Trading: {settings.trading_enabled ? 'ON' : 'OFF'}</button>
          <button className={button} onClick={() => setDraft({ ...settings })}>Settings</button>
        </>}
        <button className={button} disabled={busy || !status?.credentials_configured} onClick={() => action(() => api.deltaCheckConnection(), 'Delta account verified. Execution remains paper only.')}>Verify API connection</button>
      </div>
    </header>
    {(error || notice || status?.error || status?.history_error) && <div role="status" className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 p-3 text-sm text-[#fbbf24]">{error || notice || status?.error || status?.history_error}</div>}
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Card label="Market data" value={status?.stale ? 'Waiting for fresh trade' : `${status?.source || '--'} active`} detail={`WS ${status?.ws_connected ? 'connected' : 'disconnected'} | ${status?.proxy_configured ? 'VM proxy' : 'Direct connection'}`} />
      <Card label={`LTP (${currency})`} value={number(status?.ltp)} detail={`Last trade: ${date(status?.last_tick_at)} IST`} />
      <Card label="EMA20" value={number(status?.ema20)} detail={`Last completed candle: ${date(status?.last_bar_at)} IST`} />
      <Card label="Contract size" value={`${number(status?.contract_value)} ${status?.contract_unit || ''}`} detail={`Per contract | P&L shown in ${currency}`} />
    </div>
    {status?.ws_error && <p className="text-xs text-gray-400">{status.ws_error}</p>}
    <div className="grid gap-3 md:grid-cols-2">
      {(['BUY', 'SELL'] as const).map(side => {
        const ref = side === 'BUY' ? status?.buy_reference : status?.sell_reference;
        const time = status?.references?.find(row => row.side === side)?.time;
        const trigger = ref != null && settings ? ref + (side === 'BUY' ? 1 : -1) * settings.silver_breakout_points : undefined;
        return <div key={side} className={`rounded border p-4 ${side === 'BUY' ? 'border-[#22c55e]/40 bg-[#22c55e]/5' : 'border-[#ef4444]/40 bg-[#ef4444]/5'}`}>
          <div className={side === 'BUY' ? 'text-sm text-[#22c55e]' : 'text-sm text-[#ef4444]'}>{side} reference: {side === 'BUY' ? 'green close > EMA20' : 'red close < EMA20'}</div>
          <div className="mt-2 font-mono text-gray-100">Close {number(ref)} | Trigger {number(trigger)}</div>
          <p className="mt-2 text-xs text-gray-400">{date(time)} IST</p>
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
        </select>
      </label>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {([
          ['silver_breakout_points', 'Breakout offset'], ['sl_points', 'Initial stop loss'],
          ['target_points', 'Final target'], ['tsl_activate_points', 'TSL activates at'], ['silver_lots', 'Contracts per trade'],
        ] as const).filter(([key]) => key !== 'tsl_activate_points' || draft.exit_mode === 'target_to_breakeven_sl').map(([key, label]) => <label key={key} className="text-sm text-gray-300">{label}
          <input className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" type="number" required min={key === 'silver_lots' ? 1 : 0.000001} step={key === 'silver_lots' ? 1 : 'any'} value={draft[key]} onChange={e => setDraft({ ...draft, [key]: Number(e.target.value) })} />
        </label>)}
      </div>
      <label className="flex items-center gap-2 text-sm text-gray-300"><input type="checkbox" checked={draft.manual_exit_reentry_enabled} onChange={e => setDraft({ ...draft, manual_exit_reentry_enabled: e.target.checked })} /> Allow re-entry after manual exit when the signal qualifies</label>
      <div className="flex gap-2"><button className={button} disabled={busy}>Save</button><button className={button} type="button" onClick={() => setDraft(null)}>Cancel</button></div>
    </form>}
    <section>
      <h2 className="mb-2 text-sm font-semibold text-gray-300">OPEN PAPER POSITION</h2>
      <TradeTable rows={status?.position ? [status.position] : []} />
      {status?.position && <button className={`${button} mt-2 border-[#ef4444]/50 text-[#f87171]`} disabled={busy || status.stale} onClick={() => action(() => api.deltaClose(minutes, status.position!.id), 'Paper exit confirmed')}>Exit paper position</button>}
    </section>
    <section>
      <h2 className="mb-2 text-sm font-semibold text-gray-300">CLOSED PAPER TRADES</h2>
      <TradeTable rows={trades} closed />
      <div className="mt-2 flex justify-end gap-2"><button className={button} disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous</button><button className={button} disabled={trades.length < 100} onClick={() => setOffset(value => value + 100)}>Next</button></div>
    </section>
    <details className="panel p-3"><summary className="cursor-pointer text-sm text-[#93c5fd]">Reference history</summary>
      <div className="mt-3 max-h-80 overflow-auto"><table className="w-full text-left text-xs"><thead><tr>{['Side', 'Candle time (IST)', 'Close', 'EMA20'].map(label => <th key={label} className="p-2 text-gray-400">{label}</th>)}</tr></thead><tbody>{status?.references?.map(row => <tr key={`${row.side}-${row.time}`} className="border-t border-[#1f2937] text-gray-200"><td className="p-2">{row.side}</td><td className="p-2">{date(row.time)}</td><td className="p-2">{number(row.close)}</td><td className="p-2">{number(row.ema20)}</td></tr>)}</tbody></table></div>
    </details>
  </div>;
}

function Card({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="rounded border border-[#1f2937] bg-[#111827] p-3"><div className="text-xs uppercase tracking-wide text-gray-400">{label}</div><div className="mt-2 font-mono text-lg text-gray-100">{value}</div><p className="mt-2 text-xs text-gray-500">{detail}</p></div>;
}

function TradeTable({ rows, closed = false }: { rows: Trade[]; closed?: boolean }) {
  const headers = ['Side', 'Contracts', 'Entry time (IST)', 'Entry', 'Reference time (IST)', 'Reference', 'Initial SL', 'Current SL', 'Target', 'TSL', ...(closed ? ['Exit time (IST)', 'Exit', 'Reason', 'Gross', 'Fees', 'Net'] : ['Unrealized P&L'])];
  return <div className="max-h-[32rem] overflow-auto rounded border border-[#1f2937]"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="sticky top-0 bg-[#111827]"><tr>{headers.map(label => <th key={label} className="px-3 py-3 text-gray-400">{label}</th>)}</tr></thead><tbody>
    {!rows.length && <tr><td colSpan={headers.length} className="p-4 text-gray-500">No {closed ? 'closed trades' : 'open position'}.</td></tr>}
    {rows.map(row => <tr key={row.id} className="border-t border-[#1f2937] text-gray-200">
      <td className={`p-3 ${row.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{row.side}</td>
      {[number(row.qty), date(row.entry_time), number(row.entry_price), date(row.signal_snapshot.setup_time), number(row.signal_snapshot.setup_close), number(row.initial_sl), number(row.sl_price), number(row.target_price), row.trailing_sl_active ? 'Breakeven armed' : row.signal_snapshot.silver_breakeven ? `Arms at ${number(row.signal_snapshot.silver_breakeven.activation_price)}` : 'Fixed', ...(closed ? [date(row.exit_time), number(row.exit_price), row.exit_reason, number(row.gross_pnl), number(row.fees), number(row.net_pnl)] : [number(row.unrealized_pnl)])].map((value, index) => <td key={index} className="p-3 font-mono">{value}</td>)}
    </tr>)}
  </tbody></table></div>;
}
