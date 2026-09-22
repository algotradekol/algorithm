'use client';

import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { deltaApi, DeltaAsset } from '../lib/api';

type Settings = {
  scan_enabled: boolean; trading_enabled: boolean; silver_breakout_points: number;
  sl_points: number; target_points: number; tsl_activate_points: number; tsl_buffer_points: number;
  tsl_profit_step_points: number; tsl_lock_step_points: number; silver_lots: number;
  size_mode: 'lots' | 'pax'; pax_size: number; leverage: number;
  exit_mode: ExitMode; manual_exit_reentry_enabled: boolean;
  post_exit_cooldown_minutes: number;
  strategy_version: string;
};
export type ExitMode = 'fixed_target_sl' | 'target_to_breakeven_sl' | 'three_candle_tsl' | 'continuous_ladder_tsl';
export type Trade = {
  id: string; symbol: string; side: string; qty: number; entry_time: string; entry_price: number;
  sl_price: number; initial_sl: number; target_price: number; trailing_sl_active: boolean;
  exit_time?: string; exit_price?: number; exit_reason?: string; gross_pnl?: number; fees?: number;
  net_pnl?: number; unrealized_pnl?: number; estimated_entry_margin?: number; margin_inr?: number; pnl_inr?: number;
  exit_mode?: ExitMode;
  signal_snapshot: { setup_time?: string; setup_close?: number; trigger_level?: number; entry_candle_open?: number; silver_breakeven?: { activation_price: number; armed: boolean }; delta_three_candle_tsl?: { events?: any[]; evaluations?: any[] }; delta_ladder_tsl?: { status?: string; armed?: boolean; activation_price?: number; step_index?: number; protected_points?: number; events?: any[]; evaluations?: any[] }; silver_exit_policy?: ExitMode };
};
type TradeCell = { value: ReactNode; className?: string };
type Status = {
  settings?: Settings; symbol?: string; exchange?: string; error?: string; history_error?: string;
  ltp?: number; last_tick_at?: number; last_bar_at?: number; stale: boolean; source?: string;
  ws_connected: boolean; ws_error?: string; proxy_configured: boolean; credentials_configured: boolean;
  position?: Trade; quote_currency?: string; settlement_currency?: string; contract_value?: number; contract_unit?: string;
  ema20?: number; volume_ema20?: number; current_candle_open?: number; buy_reference?: number; sell_reference?: number; inr_rate?: number;
  summary?: { gross_pnl: number; fees: number; closed_count: number; buy_count: number; sell_count: number };
  today_summary?: { gross_pnl: number; fees: number; closed_count: number; buy_count: number; sell_count: number };
  cooldown?: { active: boolean; until?: number; remaining_seconds: number; reason?: string; pending_after_trade?: boolean; pending_duration_minutes?: number };
  references?: { side: string; time: string; open: number; high: number; low: number; close: number; volume: number; ema20: number; volume_ema20: number }[];
};

const number = (value?: number | null) => value == null ? '--' : value.toLocaleString('en-IN', { maximumFractionDigits: 6 });
const date = (value?: string | number) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '--';
const pnlClass = (value?: number | string | null) => {
  if (value == null || Number(value) === 0) return 'text-gray-200';
  return Number(value) > 0 ? 'text-[#22c55e]' : 'text-[#ef4444]';
};
const PnlValue = ({ value }: { value?: number | string | null }) => (
  <span className={`font-semibold ${pnlClass(value)}`}>{number(value == null ? null : Number(value))}</span>
);
const timeframeLabel = (minutes: number) => minutes === 60 ? '1 hr' : minutes === 120 ? '2 hr' : minutes === 240 ? '4 hr' : `${minutes} min`;
const duration = (value?: number) => {
  const seconds = Math.max(0, Math.ceil(value || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours ? `${hours}h ` : ''}${minutes}m ${seconds % 60}s`;
};
const button = 'rounded border border-[#334155] px-3 py-2 text-sm text-gray-200 hover:border-[#60a5fa] disabled:opacity-40';
const controlButton = 'rounded-md border px-2.5 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-45';
const silverDeltaDefaults = {
  silver_breakout_points: 0.10,
  sl_points: 0.30,
  target_points: 1.0,
  tsl_activate_points: 0.30,
  tsl_buffer_points: 3,
  tsl_profit_step_points: 15,
  tsl_lock_step_points: 15,
  silver_lots: 1,
  size_mode: 'lots' as const,
  pax_size: 0.001,
  leverage: 50,
  exit_mode: 'target_to_breakeven_sl' as const,
};

export default function DeltaTab({ minutes, asset = 'gold', mode = 'paper' }: { minutes: number; asset?: DeltaAsset; mode?: 'paper' | 'live' }) {
  const api = deltaApi(asset, mode);
  const metal = asset === 'silver' ? 'Silver' : 'Gold';
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
  const [pauseMenuOpen, setPauseMenuOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    setStatus(null);
    setTrades([]);
    setEditing(null);
    setEditError('');
    setNotice('');
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
  }, [asset, mode, minutes, offset, reload]);

  useEffect(() => {
    if (!editing) return;
    const current = status?.position;
    if (!current || current.id !== editing.id) {
      setEditing(null);
      setEditError('');
      return;
    }
    if (current.sl_price !== editing.sl_price || current.target_price !== editing.target_price) {
      setEditing(null);
      setEditError('');
      setNotice('Protection changed from the latest feed; reopen edit to use the current SL / target.');
    }
  }, [editing, status?.position]);

  async function action(work: () => Promise<unknown>, success: string) {
    setBusy(true); setNotice('');
    try { await work(); setNotice(success); setReload(value => value + 1); }
    catch (err) { setNotice(err instanceof Error ? err.message : 'Action failed'); }
    finally { setBusy(false); }
  }

  const settings = status?.settings;
  const currency = status?.quote_currency || 'quote currency';
  const pnl = status?.today_summary || status?.summary;
  const cooldown = status?.cooldown;
  const selectedRestMinutes = restPreset === 'custom' ? customRestMinutes : Number(restPreset);
  const canPause = !busy && Number.isInteger(selectedRestMinutes) && selectedRestMinutes >= 0 && selectedRestMinutes <= 10080;
  const estimatedLots = settings?.size_mode === 'pax' && status?.contract_value ? Math.max(1, Math.ceil(settings.pax_size / status.contract_value)) : settings?.silver_lots;
  const sizingLabel = settings?.size_mode === 'pax' ? 'PAXG per trade' : 'Lots per trade';
  const sizingValue = settings?.size_mode === 'pax' ? number(settings.pax_size) : number(settings?.silver_lots);
  const sizingDetail = settings?.size_mode === 'pax'
    ? `Approx ${number(estimatedLots)} lots | ${number(settings?.leverage)}x leverage`
    : `1 lot = ${number(status?.contract_value)} ${status?.contract_unit || ''} | ${number(settings?.leverage)}x leverage`;
  async function download(kind: 'open' | 'closed') {
    setCsvBusy(kind); setNotice('');
    try {
      const result = await api.deltaExport(minutes, kind);
      const url = URL.createObjectURL(new Blob([result.csv], { type: 'text/csv;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = result.filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(`Exported ${result.count} ${kind} ${mode} trades`);
    } catch (err) { setNotice(err instanceof Error ? err.message : 'CSV export failed'); }
    finally { setCsvBusy(null); }
  }
  async function saveProtection(sl: number, target: number, exitModeChoice?: ExitMode) {
    if (!editing) return;
    setBusy(true); setEditError('');
    try {
      // Only send exit_mode when the user actually changed it — omitting it
      // leaves the position's current mode untouched on the backend.
      const currentMode = editing.exit_mode || editing.signal_snapshot?.silver_exit_policy;
      const payload: Record<string, unknown> = {
        position_id: editing.id, sl_price: sl, target_price: target,
        expected_sl: editing.sl_price, expected_target: editing.target_price,
      };
      if (exitModeChoice && exitModeChoice !== currentMode) payload.exit_mode = exitModeChoice;
      await api.deltaProtection(minutes, payload);
      setEditing(null); setNotice(`${mode === 'live' ? 'Live Delta' : 'Paper'} SL / target saved`); setReload(v => v + 1);
    } catch (err) { setEditError(err instanceof Error ? err.message : 'Protection was not saved'); }
    finally { setBusy(false); }
  }
  function exit(row: Trade) {
    if (window.confirm(`Exit this ${row.side} ${mode} position now at the latest Delta price?${mode === 'live' ? ' This will submit a reduce-only market close to Delta.' : ''}`)) {
      void action(() => api.deltaClose(minutes, row.id), `${mode === 'live' ? 'Live Delta' : 'Paper'} exit confirmed`);
    }
  }
  return <div className="space-y-2">
    <header className="sticky top-0 z-30 -mx-1 flex flex-wrap items-end justify-between gap-2 rounded-b-xl border-b border-[#1f2937] bg-[#080d13]/95 px-1 py-1.5 shadow-[0_10px_24px_rgba(0,0,0,0.28)] backdrop-blur">
      <div>
        <h1 className="text-base font-semibold text-gray-100">Delta {metal} {timeframeLabel(minutes)} <span className={`ml-2 rounded px-2 py-0.5 text-[10px] ${mode === 'live' ? 'bg-[#ef4444]/20 text-[#f87171]' : 'bg-[#3b82f6]/20 text-[#93c5fd]'}`}>{mode === 'live' ? 'LIVE' : 'PAPER ONLY'}</span></h1>
        <p className="mt-0.5 text-[11px] text-gray-500">{status?.symbol || `${metal} symbol not configured`} | 24/7 | {asset === 'gold' ? 'EMA20 + volume EMA20' : 'Normal Silver Micro'} | No square-off</p>
      </div>
      <div className="flex flex-wrap items-center gap-1 rounded-lg border border-[#1f2937] bg-[#0b111a] p-1 lg:ml-auto lg:max-w-[calc(100%-28rem)] lg:justify-end">
        {settings && <>
          <button className={`${controlButton} ${settings.scan_enabled ? 'border-[#22c55e]/50 bg-[#22c55e]/10 text-[#22c55e]' : 'border-[#334155] text-gray-500'}`} disabled={busy} onClick={() => action(() => api.deltaSettings(minutes, { scan_enabled: !settings.scan_enabled }), 'Scan setting saved')}>Scan {settings.scan_enabled ? 'ON' : 'OFF'}</button>
          <button className={`${controlButton} ${settings.trading_enabled ? 'border-[#22c55e]/50 bg-[#22c55e]/10 text-[#22c55e]' : 'border-[#334155] text-gray-500'}`} disabled={busy} onClick={() => action(() => api.deltaSettings(minutes, { trading_enabled: !settings.trading_enabled }), `${mode === 'live' ? 'Live' : 'Paper'} trading setting saved`)}>Trade {settings.trading_enabled ? 'ON' : 'OFF'}</button>
          <button className={`${controlButton} border-[#3b82f6]/50 bg-[#3b82f6]/10 text-[#93c5fd]`} onClick={() => setDraft({ ...settings })}>Settings</button>
          <select aria-label="Entry rest duration" className={`${controlButton} border-[#334155] bg-[#0a0e14] text-gray-200`} value={restPreset} disabled={busy} onChange={e => setRestPreset(e.target.value)}>
            <option value="0">No rest</option><option value="5">5 min rest</option><option value="15">15 min rest</option><option value="30">30 min rest</option>
            <option value="60">1 hr rest</option><option value="240">4 hr rest</option><option value="720">12 hr rest</option><option value="custom">Custom minutes</option>
          </select>
          {restPreset === 'custom' && <input aria-label="Custom entry rest minutes" className={`${controlButton} w-20 border-[#334155] bg-[#0a0e14] text-gray-200`} type="number" min={0} max={10080} step={1} value={customRestMinutes} disabled={busy} onChange={e => setCustomRestMinutes(Number(e.target.value))} />}
          <div className="relative">
            <button
              className={`${controlButton} ${selectedRestMinutes ? 'border-[#f59e0b]/70 bg-[#f59e0b]/10 text-[#fbbf24]' : 'border-[#334155] text-gray-400'}`}
              disabled={!canPause}
              onClick={() => {
                if (!selectedRestMinutes) {
                  void action(() => api.deltaPause(minutes, selectedRestMinutes), 'Entry rest cleared');
                  return;
                }
                setPauseMenuOpen(value => !value);
              }}
              type="button"
            >
              {selectedRestMinutes ? 'Pause' : 'Clear rest'}
            </button>
            {pauseMenuOpen && selectedRestMinutes > 0 && <div className="absolute right-0 top-full z-50 mt-1 w-56 overflow-hidden rounded-lg border border-[#334155] bg-[#0b111a] shadow-2xl">
              <button className="block w-full px-3 py-2 text-left text-xs font-semibold text-[#fbbf24] hover:bg-[#f59e0b]/10" type="button" onClick={() => {
                setPauseMenuOpen(false);
                void action(() => api.deltaPause(minutes, selectedRestMinutes, false), `New entries paused now for ${selectedRestMinutes} minutes`);
              }}>Pause now</button>
              <button className="block w-full px-3 py-2 text-left text-xs font-semibold text-gray-200 hover:bg-[#1f2937] disabled:cursor-not-allowed disabled:opacity-45" type="button" disabled={!status?.position} onClick={() => {
                setPauseMenuOpen(false);
                void action(() => api.deltaPause(minutes, selectedRestMinutes, true), `Pause queued after the current trade exits for ${selectedRestMinutes} minutes`);
              }}>Pause after current trade</button>
            </div>}
          </div>
          <button className={`${controlButton} border-[#22c55e]/60 bg-[#22c55e]/10 text-[#22c55e]`} disabled={busy || !(cooldown?.active || cooldown?.pending_after_trade)} onClick={() => action(() => api.deltaResume(minutes), 'Entry rest cleared. The next qualifying crossing may trade.')}>Resume</button>
        </>}
        <button className={`${controlButton} border-[#334155] text-gray-300 hover:border-[#60a5fa]`} disabled={busy || !status?.credentials_configured} onClick={() => action(() => api.deltaCheckConnection(), 'Delta account verified')}>Verify API</button>
      </div>
    </header>
    {(error || notice || status?.error || status?.history_error) && <div role="status" className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 p-3 text-sm text-[#fbbf24]">{error || notice || status?.error || status?.history_error}</div>}
    {cooldown?.active && <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-[#f59e0b]/50 bg-[#f59e0b]/10 p-3">
      <div><div className="text-sm font-semibold text-[#fbbf24]">Entry rest active: {duration(cooldown.remaining_seconds)} remaining</div><p className="mt-1 text-xs text-gray-400">Started after {cooldown.reason?.replaceAll('_', ' ')}. New entries resume at {date(cooldown.until)} IST. Scanning, references, open-position exits, and EMA calculations continue.</p></div>
    </div>}
    {cooldown?.pending_after_trade && <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-[#f59e0b]/40 bg-[#f59e0b]/5 p-3">
      <div><div className="text-sm font-semibold text-[#fbbf24]">Pause queued after current trade</div><p className="mt-1 text-xs text-gray-400">The open position will keep running. After it exits, new entries pause for {cooldown.pending_duration_minutes} minutes.</p></div>
    </div>}
    <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-6">
      <Card label="Market data" value={status?.stale ? 'Waiting for fresh trade' : `${status?.source || '--'} active`} detail={`WS ${status?.ws_connected ? 'connected' : 'disconnected'} | ${status?.proxy_configured ? 'VM proxy' : 'Direct connection'}`} />
      <Card label={`LTP (${currency})`} value={number(status?.ltp)} detail={`Last trade: ${date(status?.last_tick_at)} IST`} />
      <Card label="EMA20" value={number(status?.ema20)} detail={`Last completed candle: ${date(status?.last_bar_at)} IST`} />
      <Card label="Volume EMA20" value={number(status?.volume_ema20)} detail="Completed strategy candles" />
      <Card label="Active candle open" value={number(status?.current_candle_open)} detail="Must begin on the valid side of the trigger" />
      <Card label={sizingLabel} value={sizingValue} detail={sizingDetail} />
    </div>
    {status?.ws_error && <p className="text-xs text-gray-400">{status.ws_error}</p>}
    <div className="grid gap-2 md:grid-cols-2">
      {(['BUY', 'SELL'] as const).map(side => {
        const ref = side === 'BUY' ? status?.buy_reference : status?.sell_reference;
        const time = status?.references?.find(row => row.side === side)?.time;
        const trigger = ref != null && settings ? ref + (side === 'BUY' ? 1 : -1) * settings.silver_breakout_points : undefined;
        return <div key={side} className={`rounded border px-3 py-2.5 ${side === 'BUY' ? 'border-[#22c55e]/40 bg-[#22c55e]/5' : 'border-[#ef4444]/40 bg-[#ef4444]/5'}`}>
          <div className={side === 'BUY' ? 'text-xs text-[#22c55e]' : 'text-xs text-[#ef4444]'}>{side} ref: {side === 'BUY' ? 'green close > EMA20' : 'red close < EMA20'}{asset === 'gold' ? ' + vol > vol EMA20' : ''}</div>
          <div className="mt-1 font-mono text-sm text-gray-100">Close {number(ref)} | Trigger {number(trigger)}</div>
          <p className="mt-1 text-[11px] text-gray-500">{date(time)} IST | Vol {number(status?.references?.find(row => row.side === side)?.volume)} | Vol EMA {number(status?.references?.find(row => row.side === side)?.volume_ema20)}</p>
        </div>;
      })}
    </div>
    <div className="grid gap-2 sm:grid-cols-3">
      <Card label="Closed trades today" value={number(pnl?.closed_count)} detail={`${number(pnl?.buy_count)} BUY / ${number(pnl?.sell_count)} SELL closed today`} />
      <Card label={`Today realized gross (${currency})`} value={number(pnl?.gross_pnl)} detail={`Closed ${mode} trades today only`} />
      <Card label={`Estimated net (${currency})`} value={pnl ? number(pnl.gross_pnl - pnl.fees) : '--'} detail="Product taker fees deducted; funding, taxes and slippage excluded" />
    </div>
    {draft && <form className="panel space-y-4 p-4" onSubmit={event => {
      event.preventDefault();
      void action(async () => { await api.deltaSettings(minutes, draft); setDraft(null); }, 'Delta settings saved');
    }}>
      <h2 className="font-semibold text-gray-100">{mode === 'live' ? 'Live' : 'Paper'} risk settings</h2>
      <p className="text-xs text-gray-400">All distances are in the quoted {asset} price, not rupees. Existing positions retain their entry-time protection.</p>
      {asset === 'silver' && <button type="button" className={`${button} border-[#94a3b8] text-gray-100`} onClick={() => setDraft({ ...draft, ...silverDeltaDefaults })}>Use Delta Silver defaults</button>}
      <label className="block text-sm text-gray-300">Exit mode
        <select className="mt-1 block w-full rounded border border-[#334155] bg-[#0a0e14] p-2" value={draft.exit_mode} onChange={e => setDraft({ ...draft, exit_mode: e.target.value as Settings['exit_mode'] })}>
          <option value="fixed_target_sl">Fixed Target + Fixed Stop Loss</option>
          <option value="target_to_breakeven_sl">Target + Breakeven Stop Loss</option>
          {asset === 'gold' && <option value="three_candle_tsl">Three-Candle TSL</option>}
          {asset === 'gold' && <option value="continuous_ladder_tsl">Continuous Ladder TSL</option>}
        </select>
      </label>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {([
          ['silver_breakout_points', 'Breakout offset'], ['sl_points', 'Initial stop loss'],
          ['target_points', 'Final target'], ['tsl_activate_points', draft.exit_mode === 'continuous_ladder_tsl' ? 'Target initial / breakeven at' : 'TSL activates at'], ['tsl_buffer_points', 'TSL buffer points'],
          ['tsl_profit_step_points', 'Target trailing step'], ['tsl_lock_step_points', 'SL trailing step'],
        ] as const).filter(([key]) => (key !== 'tsl_activate_points' || draft.exit_mode === 'target_to_breakeven_sl' || draft.exit_mode === 'continuous_ladder_tsl') && (key !== 'tsl_buffer_points' || draft.exit_mode === 'three_candle_tsl') && (key !== 'tsl_profit_step_points' || draft.exit_mode === 'continuous_ladder_tsl') && (key !== 'tsl_lock_step_points' || draft.exit_mode === 'continuous_ladder_tsl')).map(([key, label]) => <label key={key} className="text-sm text-gray-300">{label}
          <input className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" type="number" required min={0.000001} step="any" value={draft[key]} onChange={e => setDraft({ ...draft, [key]: Number(e.target.value) })} />
        </label>)}
      </div>
      <div className="rounded border border-[#1f2937] bg-[#0b111a] p-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="text-sm text-gray-300">Size mode
            <select className="mt-1 block w-full rounded border border-[#334155] bg-[#0a0e14] p-2" value={draft.size_mode} onChange={e => setDraft({ ...draft, size_mode: e.target.value as Settings['size_mode'] })}>
              <option value="lots">By lots</option>
              {asset === 'gold' && <option value="pax">By PAXG amount</option>}
            </select>
          </label>
          {draft.size_mode === 'pax' && asset === 'gold' ? <label className="text-sm text-gray-300">PAXG per trade
            <input className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" type="number" required min={0.000001} step="any" value={draft.pax_size} onChange={e => setDraft({ ...draft, pax_size: Number(e.target.value) })} />
          </label> : <label className="text-sm text-gray-300">Lots per trade
            <input className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" type="number" required min={1} step={1} value={draft.silver_lots} onChange={e => setDraft({ ...draft, silver_lots: Number(e.target.value) })} />
          </label>}
          <label className="text-sm text-gray-300">Leverage (x)
            <input className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" type="number" required min={0.000001} step="any" value={draft.leverage} onChange={e => setDraft({ ...draft, leverage: Number(e.target.value) })} />
          </label>
          <div className="rounded border border-[#334155]/60 bg-[#0a0e14] p-2 text-xs text-gray-400">
            <div className="uppercase tracking-wide text-gray-500">Effective Delta size</div>
            <div className="mt-1 font-mono text-sm text-gray-100">{draft.size_mode === 'pax' && asset === 'gold' && status?.contract_value ? `${number(Math.max(1, Math.ceil(draft.pax_size / status.contract_value)))} lots` : `${number(draft.silver_lots)} lots`}</div>
            <div className="mt-1">Margin estimate uses {number(draft.leverage)}x leverage.</div>
          </div>
        </div>
      </div>
      <p className="rounded border border-[#1f2937] bg-[#0a0e14] p-3 text-xs text-gray-500">Entry rest is controlled from the top bar so it can be changed quickly during live monitoring. Current saved default after manual/SL/target exits: {draft.post_exit_cooldown_minutes} min.</p>
      <div className="flex gap-2"><button className={button} disabled={busy}>Save</button><button className={button} type="button" onClick={() => setDraft(null)}>Cancel</button></div>
    </form>}
    <p className="text-xs text-gray-400">{status?.inr_rate ? `INR uses Delta India's fixed rate: 1 USD = Rs ${status.inr_rate}.` : 'INR conversion unavailable for this currency.'}</p>
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold text-gray-300">OPEN {mode.toUpperCase()} POSITION</h2><button className={`${button} border-[#22c55e]/60 text-[#22c55e]`} disabled={csvBusy !== null || !settings} onClick={() => download('open')}>{csvBusy === 'open' ? 'Exporting...' : 'Download open CSV'}</button></div>
      <TradeTable rows={status?.position ? [status.position] : []} mode={mode} disabled={busy || !status || status.stale || !!error} onEdit={row => { setEditing({ ...row }); setEditError(''); }} onExit={exit} />
    </section>
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold text-gray-300">CLOSED {mode.toUpperCase()} TRADES TODAY <span className="text-xs font-normal text-gray-500">+ last previous row</span></h2><button className={`${button} border-[#22c55e]/60 text-[#22c55e]`} disabled={csvBusy !== null || !settings} onClick={() => download('closed')}>{csvBusy === 'closed' ? 'Exporting all trades...' : 'Download closed CSV'}</button></div>
      <TradeTable rows={trades} closed />
      <div className="mt-2 flex justify-end gap-2"><button className={button} disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous</button><button className={button} disabled={trades.length < 100} onClick={() => setOffset(value => value + 100)}>Next</button></div>
    </section>
    <details className="panel p-3"><summary className="cursor-pointer text-sm text-[#93c5fd]">Reference history</summary>
      <div className="mt-3 max-h-80 overflow-auto"><table className="w-full text-left text-xs"><thead><tr>{['Side', 'Candle time (IST)', 'Open', 'High', 'Low', 'Close', 'EMA20', 'Volume', 'Volume EMA20'].map(label => <th key={label} className="p-2 text-gray-400">{label}</th>)}</tr></thead><tbody>{status?.references?.map(row => <tr key={`${row.side}-${row.time}`} className="border-t border-[#1f2937] text-gray-200"><td className="p-2">{row.side}</td><td className="p-2">{date(row.time)}</td>{[row.open, row.high, row.low, row.close, row.ema20, row.volume, row.volume_ema20].map((value, index) => <td key={index} className="p-2 font-mono">{number(value)}</td>)}</tr>)}</tbody></table></div>
    </details>
    {editing && <EditProtection key={editing.id} row={editing} mode={mode} asset={asset} ltp={status?.ltp} busy={busy} disabled={!status || status.stale || !!error || status.position?.id !== editing.id} error={editError} onClose={() => { if (!busy) setEditing(null); }} onSave={saveProtection} />}
  </div>;
}

function Card({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="rounded border border-[#1f2937] bg-[#111827] px-3 py-2"><div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div><div className="mt-1 font-mono text-base text-gray-100">{value}</div><p className="mt-1 text-[11px] leading-snug text-gray-500">{detail}</p></div>;
}

export function TradeTable({ rows, closed = false, mode = 'paper', disabled, onEdit, onExit }: { rows: Trade[]; closed?: boolean; mode?: 'paper' | 'live'; disabled?: boolean; onEdit?: (row: Trade) => void; onExit?: (row: Trade) => void }) {
  const headers = ['Side', 'Lots', 'Entry time (IST)', 'Entry', 'Reference time (IST)', 'Reference', 'Initial SL', 'Current SL', 'Target', ...(!closed ? ['Edit'] : []), 'TSL', ...(closed ? ['Exit time (IST)', 'Exit', 'Reason', 'Gross', 'Net', 'Net (INR)'] : ['Unrealized P&L', 'Unrealized (INR)', 'Exit'])];
  return <div className="max-h-[32rem] overflow-auto rounded border border-[#1f2937]">
    <table className="w-full whitespace-nowrap text-left text-xs">
      <thead className="sticky top-0 bg-[#111827]">
        <tr>{headers.map(label => <th key={label} className="px-3 py-3 text-gray-400">{label}</th>)}</tr>
      </thead>
      <tbody>
        {!rows.length && <tr><td colSpan={headers.length} className="p-4 text-gray-500">No {closed ? 'closed trades' : `open ${mode} position`}.</td></tr>}
        {rows.map(row => {
          const baseCells: TradeCell[] = [
            { value: number(row.qty) },
            { value: date(row.entry_time) },
            { value: number(row.entry_price) },
            { value: date(row.signal_snapshot.setup_time) },
            { value: number(row.signal_snapshot.setup_close) },
            { value: number(row.initial_sl) },
            { value: number(row.sl_price) },
            { value: number(row.target_price) },
          ];
          const editCell: TradeCell = { value: <button className="min-h-9 rounded border border-[#3b82f6]/70 px-2.5 py-1.5 text-xs font-semibold text-[#3b82f6] disabled:opacity-40" disabled={disabled} onClick={() => onEdit?.(row)}>Edit</button> };
          const exitCell: TradeCell = { value: <button className="min-h-9 rounded border border-[#ef4444]/70 px-2.5 py-1.5 text-xs font-semibold text-[#ef4444] disabled:opacity-40" disabled={disabled} onClick={() => onExit?.(row)}>Exit</button> };
          const cells: TradeCell[] = [
            ...baseCells,
            ...(!closed ? [editCell] : []),
            { value: <TslAudit key="tsl" row={row} /> },
            ...(closed
              ? [
                { value: date(row.exit_time) },
                { value: number(row.exit_price) },
                { value: row.exit_reason },
                { value: <PnlValue value={row.gross_pnl} /> },
                { value: <PnlValue value={row.net_pnl} /> },
                { value: <PnlValue value={row.pnl_inr} /> },
              ]
              : [
                { value: <PnlValue value={row.unrealized_pnl} /> },
                { value: <PnlValue value={row.pnl_inr} /> },
                exitCell,
              ]),
          ];
          return <tr key={row.id} className="border-t border-[#1f2937] text-gray-200">
            <td className={`p-3 ${row.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{row.side}</td>
            {cells.map((cell, index) => <td key={index} className={`p-3 font-mono ${cell.className || ''}`}>{cell.value}</td>)}
          </tr>;
        })}
      </tbody>
    </table>
  </div>;
}

function TslAudit({ row }: { row: Trade }) {
  const three = row.signal_snapshot.delta_three_candle_tsl;
  if (three) {
    const events = three.events || [];
    const evaluations = three.evaluations || [];
    const latest = evaluations[evaluations.length - 1] || events[events.length - 1];
    return <details><summary className="cursor-pointer text-[#a78bfa]">Three-candle {evaluations.length ? `${evaluations.length} check(s)` : 'waiting'}{events.length ? ` / ${events.length} move(s)` : ''}</summary>{latest && <div className="mt-2 min-w-80 space-y-1 text-[11px] text-gray-400"><div>Status {latest.status || 'checked'} | Candidate {number(latest.candidate_sl)} from {number(latest.reference_price)} with buffer {number(latest.buffer_points)}</div>{latest.candles?.map((candle: any) => <div key={candle.time}>{date(candle.time)} | O {number(candle.open)} H {number(candle.high)} L {number(candle.low)} C {number(candle.close)}</div>)}</div>}</details>;
  }
  const ladder = row.signal_snapshot.delta_ladder_tsl;
  if (ladder) {
    const events = ladder.events || [];
    const evaluations = ladder.evaluations || [];
    const latest = evaluations[evaluations.length - 1] || events[events.length - 1];
    const label = ladder.armed
      ? `Ladder step ${Math.max(0, Number(ladder.step_index ?? 0))} / locked ${number(ladder.protected_points)}`
      : `Ladder waits at ${number(ladder.activation_price)}`;
    return <details><summary className="cursor-pointer text-[#fbbf24]">{label}</summary>{latest && <div className="mt-2 min-w-72 space-y-1 text-[11px] text-gray-400"><div>Status {latest.status || ladder.status || 'checked'} | LTP {number(latest.ltp)} | SL {number(latest.previous_sl)} → {number(latest.new_sl)}</div><div>Gain {number(latest.gain_points)} | Locked {number(latest.protected_points)} | Step {number(latest.step_index)}</div></div>}</details>;
  }
  if (row.trailing_sl_active) return <>Breakeven armed</>;
  if (row.signal_snapshot.silver_breakeven) return <>Arms at {number(row.signal_snapshot.silver_breakeven.activation_price)}</>;
  return <>Fixed</>;
}

function EditProtection({ row, mode, asset, ltp, busy, disabled, error, onClose, onSave }: { row: Trade; mode: 'paper' | 'live'; asset: DeltaAsset; ltp?: number; busy: boolean; disabled: boolean; error: string; onClose: () => void; onSave: (sl: number, target: number, exitMode?: ExitMode) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [sl, setSl] = useState(String(row.sl_price));
  const [target, setTarget] = useState(String(row.target_price));
  const currentMode: ExitMode = (row.exit_mode || row.signal_snapshot?.silver_exit_policy || 'fixed_target_sl') as ExitMode;
  const [exitMode, setExitMode] = useState<ExitMode>(currentMode);
  useEffect(() => { dialog.current?.showModal(); }, []);
  // Silver cannot use three_candle_tsl — validator blocks it server-side too.
  const modeOptions: { value: ExitMode; label: string }[] = [
    { value: 'fixed_target_sl', label: 'Fixed SL + target' },
    { value: 'target_to_breakeven_sl', label: 'Target to breakeven SL' },
    ...(asset === 'gold' ? [{ value: 'three_candle_tsl' as ExitMode, label: 'Three-candle TSL' }] : []),
    ...(asset === 'gold' ? [{ value: 'continuous_ladder_tsl' as ExitMode, label: 'Continuous Ladder TSL' }] : []),
  ];
  const modeChanged = exitMode !== currentMode;
  return <dialog ref={dialog} onCancel={e => { e.preventDefault(); onClose(); }} aria-labelledby="delta-protection-title" className="w-[calc(100%_-_2rem)] max-w-md rounded border border-[#1f2937] bg-[#0d1117] p-4 text-gray-100 backdrop:bg-black/70">
    <form onSubmit={e => { e.preventDefault(); onSave(Number(sl), Number(target), exitMode); }}>
      <div className="flex items-start justify-between gap-3"><div><h3 id="delta-protection-title" className="font-semibold">Edit SL / Target</h3><p className="mt-1 text-xs text-gray-400">{row.symbol} | {row.side} | Entry {number(row.entry_price)} | LTP {number(ltp)}</p></div><button type="button" aria-label="Close editor" disabled={busy} onClick={onClose}>×</button></div>
      <p className="mt-3 rounded border border-[#3b82f6]/40 p-2 text-xs text-[#93c5fd]">{mode === 'live' ? 'Live Delta position. Saving will amend the tracked Delta stop/target orders first, then update the app.' : 'Paper position only.'} Enter absolute price levels, not distances. Settings and other positions are unchanged. TSL activation stays at its captured price and will not loosen a tighter manual stop.</p>
      <label className="mt-4 block text-sm text-gray-300">TSL / exit mode
        <select className="mt-1 block w-full rounded border border-[#334155] bg-[#0a0e14] p-2 text-sm" value={exitMode} disabled={busy} onChange={e => setExitMode(e.target.value as ExitMode)}>
          {modeOptions.map(opt => <option key={opt.value} value={opt.value}>{opt.label}{opt.value === currentMode ? ' (current)' : ''}</option>)}
        </select>
      </label>
      {modeChanged && <p className="mt-2 rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 p-2 text-xs text-[#fbbf24]">
        Switching from <b>{currentMode}</b> to <b>{exitMode}</b> will rewrite this position&rsquo;s trail state. The new mode starts fresh from the current SL / target you save here.
      </p>}
      <div className="mt-4 grid grid-cols-2 gap-3">{[['Stop loss', sl, setSl], ['Target', target, setTarget]].map(([label, value, setter]) => <label key={label as string} className="text-sm text-gray-300">{label as string}<input autoFocus={label === 'Stop loss'} type="number" min="0.000001" step="any" required value={value as string} disabled={busy} onChange={e => (setter as (value: string) => void)(e.target.value)} className="mt-1 w-full rounded border border-[#334155] bg-[#0a0e14] p-2" /></label>)}</div>
      {(error || disabled) && <p role="alert" className="mt-3 text-sm text-[#f87171]">{error || 'Fresh price or matching open position unavailable. Reload before editing.'}</p>}
      <div className="mt-4 flex justify-end gap-2"><button className={button} type="button" disabled={busy} onClick={onClose}>Cancel</button><button className={`${button} border-[#3b82f6] bg-[#3b82f6]/20`} disabled={busy || disabled}>{busy ? 'Saving...' : 'Save'}</button></div>
    </form>
  </dialog>;
}
