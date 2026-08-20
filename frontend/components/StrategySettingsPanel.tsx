'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

type Field = [string, string, string];

const CAPITAL_FIELDS: Field[] = [
  ['starting_capital', 'Starting Capital (Rs)', 'baseline capital shown in strategy summary'],
  ['capital_per_trade', 'Capital Per Trade (Rs)', 'paper capital allocated to one new trade'],
  ['margin_multiplier', 'Margin Cap (x)', 'ceiling only. Actual leverage is per-stock from the broker approved list (1x–5x); this caps it. Keep at 5 to use each stock’s full approved margin.'],
];

const RISK_FIELDS: Field[] = [
  ['target_pct', 'Target % (per trade)', 'BUY: +value (e.g. +2). SELL: −value (e.g. −2). Sign is a display convention; magnitude is what applies.'],
  ['sl_pct', 'Stop Loss % (per trade)', 'BUY: −value (e.g. −1). SELL: +value (e.g. +1). Sign is a display convention; magnitude is what applies.'],
  ['trailing_sl_trigger_pct', 'Trailing SL Trigger %', 'start trailing after price moves this much in favor'],
  ['trailing_sl_distance_pct', 'Trailing SL Distance %', 'trail stop this far behind the best favorable price'],
  ['max_trades_per_day', 'Max Trades Per Day', 'daily total trade cap'],
  ['max_buy_trades', 'Max Buy Trades Per Day', 'daily buy-side trade cap'],
  ['max_sell_trades', 'Max Sell Trades Per Day', 'daily sell-side trade cap'],
];

// algo3 (Silver Micro) uses POINTS instead of %, per spec doc. Separate
// field list keeps the UI honest about units.
const SILVER_RISK_FIELDS: Field[] = [
  ['silver_lots', 'Lots per trade', 'Position size in lots. 1 lot of SILVERMIC = 1 kg = 1 unit on Fyers. Default 1.'],
  ['silver_breakout_points', 'Breakout Offset n (points)', 'Entry fires when live price crosses (setup close +/- n). Default 150.'],
  ['target_points', 'Target (points from entry)', 'Fixed rupee distance in favor before exit.'],
  ['sl_points', 'Stop Loss (points from entry)', 'Fixed rupee distance against before exit.'],
  ['tsl_trigger_points', 'Trailing SL Trigger (points)', 'Profit in points before trailing activates.'],
  ['tsl_distance_points', 'Trailing SL Distance (points)', 'How far behind the best favorable price the trailing stop sits once active.'],
];

const INDICATOR_FIELDS: Field[] = [
  ['rsi_buy_threshold', 'RSI Buy Threshold', 'Filter strategy buy confirmation threshold'],
  ['rsi_sell_threshold', 'RSI Sell Threshold', 'Filter strategy sell confirmation threshold'],
  ['adx_threshold', 'ADX Threshold', 'minimum trend strength for filter strategy'],
  ['min_volume', 'Min Volume', 'minimum 9:15 candle volume'],
  ['min_total_value', 'Min Total Value (Rs)', 'minimum traded value for the day'],
  ['ltp_min', 'LTP Min (Rs)', 'minimum allowed entry price'],
  ['ltp_max', 'Sell LTP Max (Rs)', 'v14 uses this sell-side ceiling; BUY is capped at Rs 3,000'],
  ['supertrend_period', 'Supertrend Period', 'ATR period used by Supertrend'],
  ['supertrend_multiplier', 'Supertrend Multiplier', 'ATR multiplier used by Supertrend'],
];

const FILTERS: [string, string, string, string][] = [
  ['filter_vwap', 'VWAP Filter', "Price must be above/below the day's running VWAP", ''],
  ['filter_rsi', 'RSI Filter', 'Momentum confirmation - RSI above/below threshold', 'Threshold: buy / sell'],
  ['filter_adx', 'ADX Filter', 'Trend strength - filters out sideways/choppy stocks', 'Threshold'],
  ['filter_supertrend', 'Supertrend Filter', 'Price must be above/below Supertrend line', 'Period / Mult'],
  ['filter_ema20', 'EMA20 Filter', 'Price above/below 20-period EMA using pre-warmed 1-minute candles', 'Pre-warmed'],
  ['filter_ema50', 'EMA50 Filter', 'EMA20 must be above/below EMA50 using pre-warmed 1-minute candles', 'Pre-warmed'],
  ['filter_volume', 'Volume Filter', 'Minimum shares traded in the 9:15 candle', 'Min volume'],
  ['filter_liquidity', 'Liquidity Filter', 'Minimum total traded value for the day', 'Min value'],
  ['filter_price_range', 'Price Range Filter', 'Avoids penny stocks and very expensive stocks', 'Min / Max'],
];

const EXIT_MODES = [
  ['fixed_target_sl', 'Fixed Target + SL', 'Exit at fixed target, normal SL, or EOD. Trailing is ignored.'],
  ['trailing_sl_only', 'Trailing SL Only', 'No fixed target exit. Winners run until trailing/normal SL or EOD.'],
  ['fixed_target_trailing_sl', 'Fixed Target + Trailing SL', 'Exit at target, or let trailing SL protect profit if price reverses first.'],
];

export default function StrategySettingsPanel({ algoId, tradingMode }: { algoId: string; tradingMode?: 'paper' | 'live' }) {
  const isLive = tradingMode === 'live';
  const [settings, setSettings] = useState<Record<string, any> | null>(null);
  const [availableCash, setAvailableCash] = useState('');
  const [cashSaving, setCashSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const defaultsLabel = 'Reset to Tradetron defaults';

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.getSettings(algoId), api.summary(algoId)]).then(([result, summary]) => {
      if (!cancelled) {
        setSettings(result);
        setAvailableCash(formatInputMoney(summary.cash));
        setError('');
      }
    }).catch((e: any) => {
      if (!cancelled) setError(e?.message || 'Failed to load strategy settings');
    });
    return () => { cancelled = true; };
  }, [algoId]);

  async function save() {
    if (!settings) return;
    try {
      await api.updateSettings(algoId, settings);
      setSaved(true);
      setError('');
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e?.message || 'Failed to save strategy settings');
    }
  }

  async function resetDefaults() {
    try {
      const result = await api.resetSettings(algoId);
      setSettings(result);
      setSaved(true);
      setError('');
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e?.message || 'Failed to reset strategy settings');
    }
  }

  async function saveAvailableCash() {
    const cash = Number(availableCash);
    if (!Number.isFinite(cash) || cash < 0) {
      setError('Available cash must be zero or greater.');
      return;
    }
    setCashSaving(true);
    try {
      const result = await api.updateAvailableCash(algoId, roundMoney(cash));
      setAvailableCash(formatInputMoney(result.cash));
      setSaved(true);
      setError('');
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e?.message || 'Failed to update available cash');
    } finally {
      setCashSaving(false);
    }
  }

  if (!settings) return <p className="text-sm text-gray-500">Loading strategy settings...</p>;

  const preview = calculatePreview(settings);

  return (
    <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(340px,0.8fr)]">
      <div className="panel space-y-4 p-4">
        <div className="rounded border border-[#1f2937] bg-[#0d1117] px-3 py-3">
          <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
            <div>
              <div className="text-sm font-semibold text-gray-100">Strategy Settings</div>
              <p className="mt-1 text-xs text-gray-500">Adjust the algo rules, capital, and risk controls from one dashboard-styled panel.</p>
            </div>
            <div className="label text-[10px] text-gray-500">Changes save to paper or live based on the active trading mode.</div>
          </div>
        </div>
        <div className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">
          Changes apply to new trades only. Open positions keep their original entry prices, SL, and targets.
        </div>
        {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}

        {!isLive && <CashControl value={availableCash} setValue={setAvailableCash} onSave={saveAvailableCash} saving={cashSaving} />}
        <FieldGroup
          title="Capital Settings"
          fields={isLive ? CAPITAL_FIELDS.filter(([key]) => key !== 'starting_capital') : CAPITAL_FIELDS}
          settings={settings}
          setSettings={setSettings}
        />
        {isLive && (
          <p className="mt-2 rounded border border-[#3b82f6]/40 bg-[#3b82f6]/10 px-3 py-2 text-xs text-[#93c5fd]">
            Live mode: wallet balance comes from Fyers in real time. "Available Cash" and "Starting Capital" are paper-mode-only and hidden here.
          </p>
        )}
        <OrderTypeSelect settings={settings} setSettings={setSettings} />
        {isLive && <ParallelPaperToggle settings={settings} setSettings={setSettings} />}
        {(algoId === 'algo2' || algoId === 'algo3') && (
          <ScanToggle algoId={algoId} settings={settings} setSettings={setSettings} />
        )}
        <ExitModeSelect settings={settings} setSettings={setSettings} />
        <TrailingStopToggle settings={settings} setSettings={setSettings} />
        <FieldGroup
          title="Risk Settings"
          fields={algoId === 'algo3' ? SILVER_RISK_FIELDS : RISK_FIELDS}
          settings={settings}
          setSettings={setSettings}
        />
          {algoId === 'algo3' && (
            <div className="mt-5 rounded border border-[#3b82f6]/40 bg-[#3b82f6]/10 px-3 py-2 text-xs text-[#93c5fd]">
            Silver Micro runs on the front-month MCX SILVERMIC contract using 15-minute candles. Setup levels come from green/red candles crossing EMA20; entry fires when live price crosses setup close +/- n points (default 150), including gap-opens past the level and candle-closes that clear the level even when live ticks were sparse. Position size is in LOTS (1 lot = 1 kg). SL, target, and trailing SL are all in POINTS from entry. Default order type is MARKET.
            </div>
          )}
        {(algoId === 'algo1' || algoId === 'algo4') && <TestSchedule settings={settings} setSettings={setSettings} />}
        {algoId === 'algo2' && (
          <IndicatorFilterSettings settings={settings} setSettings={setSettings} />
        )}

        <div className="mt-5 grid gap-2 sm:grid-cols-[1fr_auto]">
          <button
            onClick={save}
            className="inline-flex min-h-10 w-full items-center justify-center gap-2 rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2.5 text-sm font-semibold text-white"
          >
            <i className="ri-save-fill text-sm text-white" />
            {saved ? 'Saved' : 'Save settings'}
          </button>
          <button
            onClick={resetDefaults}
            className="inline-flex min-h-10 items-center justify-center gap-2 rounded border border-[#f59e0b]/70 bg-[#f59e0b]/10 px-4 py-2.5 text-sm font-semibold text-[#f59e0b]"
          >
            <i className="ri-refresh-fill text-sm text-[#f59e0b]" />
            {defaultsLabel}
          </button>
        </div>
      </div>

      <aside className="panel p-4">
        <h3 className="text-base font-semibold text-gray-100">Live Strategy Preview</h3>
        <p className="mt-2 text-xs text-gray-500">Assumes example price Rs 500 at full {Number(settings.margin_multiplier || 5)}x cap.</p>
        <div className="mt-4 divide-y divide-[#1f2937] border-y border-[#1f2937] text-sm">
          <PreviewRow label="Position size (at cap)" value={`${preview.positionSize.toLocaleString('en-IN')} qty`} />
          <PreviewRow label="Effective capital at cap" value={formatMoney(preview.effectiveCapital)} />
          <PreviewRow label="Max daily risk" value={formatMoney(preview.maxDailyRisk)} tone="text-[#ef4444]" />
          <PreviewRow label="Max daily reward" value={formatMoney(preview.maxDailyReward)} tone="text-[#22c55e]" />
        </div>
        <p className="mt-3 text-xs text-gray-500">Actual quantity is sized per stock: capital × that stock’s broker-approved margin (1x–5x), so lower-margin names get proportionally fewer shares.</p>
      </aside>
    </section>
  );
}

function TestSchedule({ settings, setSettings }: { settings: Record<string, any>; setSettings: (settings: Record<string, any>) => void }) {
  const enabled = Boolean(settings.test_schedule_enabled);
  return (
    <div className="mt-5 rounded border border-[#1f2937] bg-[#111827] p-3">
      <label className="flex gap-3">
        <input type="checkbox" checked={enabled} onChange={(e) => setSettings({ ...settings, test_schedule_enabled: e.target.checked })} className="peer sr-only" />
        <span className="mt-1 h-5 w-9 shrink-0 rounded-full border border-[#1f2937] bg-gray-700 after:block after:h-4 after:w-4 after:translate-x-0.5 after:translate-y-0.5 after:rounded-full after:bg-gray-400 after:transition peer-checked:bg-[#f59e0b] peer-checked:after:translate-x-4 peer-checked:after:bg-white" />
        <span><span className="text-sm font-semibold text-gray-100">Test Schedule</span><span className="mt-1 block text-xs text-gray-500">Uses a future intraday candle for a paper-only pipeline check. Turn this off to restore the 09:15 production schedule.</span></span>
      </label>
      {enabled && <label className="mt-3 block"><div className="label">Test Window Start (IST)</div><input type="time" value={settings.test_candle_time || '11:10'} onChange={(e) => setSettings({ ...settings, test_candle_time: e.target.value })} className="control mt-1" /><p className="mt-1 text-xs text-[#f59e0b]">The strategy collects three closed 1-minute candles from this time, ranks the combined range, then enters during the next minute. It still compares against the previous-day close, so this is a systems test, not a valid opening-gap trade signal.</p></label>}
    </div>
  );
}

function ScanToggle({
  algoId,
  settings,
  setSettings,
}: {
  algoId: string;
  settings: Record<string, any>;
  setSettings: (settings: Record<string, any>) => void;
}) {
  const enabled = Boolean(settings.scan_enabled);
  const helper =
    algoId === 'algo2'
      ? 'When off, the Filter strategy will not run its scan or place new entries.'
      : 'When off, Silver Micro will keep diagnostics and history warm, but it will not create new setups or entries.';

  return (
    <div className="mt-5 rounded border border-[#1f2937] bg-[#111827] p-3">
      <label className="flex gap-3">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setSettings({ ...settings, scan_enabled: e.target.checked })}
          className="peer sr-only"
        />
        <span className="mt-1 h-5 w-9 shrink-0 rounded-full border border-[#1f2937] bg-gray-700 after:block after:h-4 after:w-4 after:translate-x-0.5 after:translate-y-0.5 after:rounded-full after:bg-gray-400 after:transition peer-checked:bg-[#22c55e] peer-checked:after:translate-x-4 peer-checked:after:bg-white" />
        <span>
          <span className="text-sm font-semibold text-gray-100">Enable Scanning</span>
          <span className="mt-1 block text-xs text-gray-500">{helper}</span>
        </span>
      </label>
    </div>
  );
}

function OrderTypeSelect({
  settings,
  setSettings,
}: {
  settings: Record<string, any>;
  setSettings: (settings: Record<string, any>) => void;
}) {
  const current = String(settings.order_type || 'LIMIT').toUpperCase();
  const options: [string, string, string][] = [
    ['LIMIT', 'Limit @ LTP', 'Entry order is placed as LIMIT at the live LTP snapshot. Safer, but may go unfilled if price moves away before Fyers receives it.'],
    ['MARKET', 'Market', 'Entry order is placed as MARKET. Guaranteed fill, but slippage on fast-moving stocks.'],
  ];
  return (
    <div className="mt-5 rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="label mb-3">Entry Order Type</div>
      <div className="grid gap-2 sm:grid-cols-2">
        {options.map(([value, label, helper]) => (
          <label key={value} className={`rounded border p-3 ${
            current === value ? 'border-[#3b82f6] bg-[#3b82f6]/10' : 'border-[#1f2937] bg-[#0d1117]'
          }`}>
            <div className="flex items-start gap-2">
              <input
                type="radio"
                name="order_type"
                checked={current === value}
                onChange={() => setSettings({ ...settings, order_type: value })}
                className="mt-1"
              />
              <span>
                <span className="block text-sm font-semibold text-gray-100">{label}</span>
                <span className="mt-1 block text-xs text-gray-500">{helper}</span>
              </span>
            </div>
          </label>
        ))}
      </div>
    </div>
  );
}

function ExitModeSelect({
  settings,
  setSettings,
}: {
  settings: Record<string, any>;
  setSettings: (settings: Record<string, any>) => void;
}) {
  return (
    <div className="mt-5 rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="label mb-3">Exit Mode</div>
      <div className="grid gap-2">
        {EXIT_MODES.map(([value, label, helper]) => (
          <label key={value} className={`rounded border p-3 ${
            settings.exit_mode === value ? 'border-[#3b82f6] bg-[#3b82f6]/10' : 'border-[#1f2937] bg-[#0d1117]'
          }`}>
            <div className="flex items-start gap-2">
              <input
                type="radio"
                name={`exit_mode_${settings.algo_id || 'algo'}`}
                checked={settings.exit_mode === value}
                onChange={() => setSettings({ ...settings, exit_mode: value })}
                className="mt-1"
              />
              <span>
                <span className="block text-sm font-semibold text-gray-100">{label}</span>
                <span className="mt-1 block text-xs text-gray-500">{helper}</span>
              </span>
            </div>
          </label>
        ))}
      </div>
    </div>
  );
}

function ParallelPaperToggle({
  settings,
  setSettings,
}: {
  settings: Record<string, any>;
  setSettings: (settings: Record<string, any>) => void;
}) {
  const on = Boolean(settings.parallel_paper_enabled);
  return (
    <label className="mt-5 flex gap-3 rounded border border-[#22c55e]/40 bg-[#22c55e]/5 p-3">
      <input
        type="checkbox"
        checked={on}
        onChange={(e) => setSettings({ ...settings, parallel_paper_enabled: e.target.checked })}
        className="peer sr-only"
      />
      <span className="mt-1 h-5 w-9 shrink-0 rounded-full border border-[#1f2937] bg-gray-700 after:block after:h-4 after:w-4 after:translate-x-0.5 after:translate-y-0.5 after:rounded-full after:bg-gray-400 after:transition peer-checked:bg-[#22c55e] peer-checked:after:translate-x-4 peer-checked:after:bg-white" />
      <span className="flex-1">
        <span className="text-sm font-semibold text-gray-100">Parallel Paper Trading</span>
        <span className="mt-1 block text-xs text-gray-500">
          When ON, every LIVE entry also mirrors as a paper trade with fake money in the paper tables.
          Compare live vs paper results side-by-side by toggling the Mode switch at the top — your
          real live positions are untouched. Turn OFF if you don't want the extra paper simulations.
        </span>
      </span>
    </label>
  );
}

function TrailingStopToggle({
  settings,
  setSettings,
}: {
  settings: Record<string, any>;
  setSettings: (settings: Record<string, any>) => void;
}) {
  const modeUsesTrailing = settings.exit_mode === 'trailing_sl_only' || settings.exit_mode === 'fixed_target_trailing_sl';
  return (
    <label className={`mt-5 flex gap-3 rounded border border-[#1f2937] bg-[#111827] p-3 ${modeUsesTrailing ? '' : 'opacity-60'}`}>
      <input
        type="checkbox"
        checked={Boolean(settings.trailing_sl_enabled)}
        disabled={!modeUsesTrailing}
        onChange={(e) => setSettings({ ...settings, trailing_sl_enabled: e.target.checked })}
        className="peer sr-only"
      />
      <span className="mt-1 h-5 w-9 rounded-full border border-[#1f2937] bg-gray-700 after:block after:h-4 after:w-4 after:translate-x-0.5 after:translate-y-0.5 after:rounded-full after:bg-gray-400 after:transition peer-checked:bg-[#3b82f6] peer-checked:after:translate-x-4 peer-checked:after:bg-white" />
      <span className="flex-1">
        <span className="text-sm font-semibold text-gray-100">Trailing Stop Loss</span>
        <span className="mt-1 block text-xs text-gray-500">
          {modeUsesTrailing
            ? 'Per-algo toggle. Once profit reaches the trigger, SL follows the best favorable price by the configured distance.'
            : 'Choose an exit mode that includes trailing SL to enable this.'}
        </span>
      </span>
    </label>
  );
}

function CashControl({
  value,
  setValue,
  onSave,
  saving,
}: {
  value: string;
  setValue: (value: string) => void;
  onSave: () => void;
  saving: boolean;
}) {
  return (
    <div className="mt-5 rounded border border-[#1f2937] bg-[#111827] p-3 first:mt-0">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="flex-1">
          <div className="label">Available Cash (Rs)</div>
          <input
            type="number"
            min="0"
            step="0.01"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onBlur={(e) => setValue(formatInputMoney(e.target.value))}
            className="control mt-1 num"
          />
          <div className="mt-1 text-xs text-gray-500">Updates the Cash Available card for this algo only. This does not delete trades or reset daily limits.</div>
        </label>
        <button onClick={onSave} disabled={saving} className="inline-flex min-h-10 items-center justify-center gap-2 rounded border border-[#22c55e] bg-[#22c55e] px-4 py-2 text-sm font-semibold text-[#07130b] disabled:opacity-50">
          <i className="ri-wallet-3-fill text-sm" />
          {saving ? 'Updating...' : 'Set cash'}
        </button>
      </div>
    </div>
  );
}

function IndicatorFilterSettings({
  settings,
  setSettings,
}: {
  settings: Record<string, any>;
  setSettings: (settings: Record<string, any>) => void;
}) {
  const filterFields: Record<string, Field[]> = {
    filter_rsi: INDICATOR_FIELDS.filter(([key]) => key === 'rsi_buy_threshold' || key === 'rsi_sell_threshold'),
    filter_adx: INDICATOR_FIELDS.filter(([key]) => key === 'adx_threshold'),
    filter_supertrend: INDICATOR_FIELDS.filter(([key]) => key === 'supertrend_period' || key === 'supertrend_multiplier'),
    filter_volume: INDICATOR_FIELDS.filter(([key]) => key === 'min_volume'),
    filter_liquidity: INDICATOR_FIELDS.filter(([key]) => key === 'min_total_value'),
    filter_price_range: INDICATOR_FIELDS.filter(([key]) => key === 'ltp_min' || key === 'ltp_max'),
  };
  return (
    <div className="mt-5 rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="label mb-3">Indicator Filters</div>
      <div className="space-y-2">
        {FILTERS.map(([key, label, helper, meta]) => (
          <div key={key} className="rounded border border-[#1f2937] bg-[#0d1117]">
            <label className="flex gap-3 p-3">
              <input type="checkbox" checked={Boolean(settings[key])} onChange={(e) => setSettings({ ...settings, [key]: e.target.checked })} className="peer sr-only" />
              <span className="mt-1 h-5 w-9 shrink-0 rounded-full border border-[#1f2937] bg-gray-700 after:block after:h-4 after:w-4 after:translate-x-0.5 after:translate-y-0.5 after:rounded-full after:bg-gray-400 after:transition peer-checked:bg-[#3b82f6] peer-checked:after:translate-x-4 peer-checked:after:bg-white" />
              <span className="flex-1"><span className="flex flex-wrap items-center gap-2 text-sm font-semibold text-gray-100">{label}{meta && <span className="rounded border border-[#1f2937] px-2 py-0.5 text-[10px] uppercase tracking-wider text-gray-500">{meta}</span>}</span><span className="mt-1 block text-xs text-gray-500">{helper}</span></span>
            </label>
            {settings[key] && filterFields[key]?.length ? (
              <details className="border-t border-[#1f2937]" open>
                <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-[#3b82f6]">Filter thresholds</summary>
                <div className="grid gap-3 border-t border-[#1f2937] p-3 md:grid-cols-2">
                  {filterFields[key].map(([fieldKey, fieldLabel, fieldHelper]) => <NumberField key={fieldKey} fieldKey={fieldKey} label={fieldLabel} helper={fieldHelper} settings={settings} setSettings={setSettings} />)}
                </div>
              </details>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function FieldGroup({
  title,
  fields,
  settings,
  setSettings,
}: {
  title: string;
  fields: Field[];
  settings: Record<string, any>;
  setSettings: (settings: Record<string, any>) => void;
}) {
  return (
    <div className="mt-5 rounded border border-[#1f2937] bg-[#111827] p-3 first:mt-0">
      <div className="label mb-3">{title}</div>
      <div className="grid gap-3 md:grid-cols-2">
        {fields.map(([key, label, helper]) => (
          <NumberField key={key} fieldKey={key} label={label} helper={helper} settings={settings} setSettings={setSettings} />
        ))}
      </div>
    </div>
  );
}

function NumberField({ fieldKey, label, helper, settings, setSettings }: { fieldKey: string; label: string; helper: string; settings: Record<string, any>; setSettings: (settings: Record<string, any>) => void }) {
  const integerFields = new Set(['max_trades_per_day', 'max_buy_trades', 'max_sell_trades', 'supertrend_period', 'min_volume', 'silver_lots', 'silver_breakout_points', 'sl_points', 'target_points', 'tsl_trigger_points', 'tsl_distance_points']);
  const rupeeFields = new Set(['starting_capital', 'capital_per_trade', 'min_total_value', 'ltp_min', 'ltp_max']);
  const signedFields = new Set(['sl_pct', 'target_pct']);
  const step = integerFields.has(fieldKey) ? '1' : rupeeFields.has(fieldKey) ? '0.01' : '0.0001';
  const allowNegative = signedFields.has(fieldKey);
  return <label><div className="label">{label}</div><input type="number" step={step} {...(allowNegative ? {} : { min: '0' })} value={Number.isFinite(settings[fieldKey]) ? settings[fieldKey] : 0} onChange={(e) => setSettings({ ...settings, [fieldKey]: Number(e.target.value) || 0 })} onBlur={(e) => setSettings({ ...settings, [fieldKey]: roundForField(fieldKey, Math.abs(Number(e.target.value) || 0)) })} className="control mt-1 num" /><div className="mt-1 text-xs text-gray-500">{helper}</div></label>;
}

function PreviewRow({ label, value, tone = 'text-gray-100' }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <span className="text-xs uppercase tracking-wider text-gray-500">{label}</span>
      <span className={`num text-sm font-semibold ${tone}`}>{value}</span>
    </div>
  );
}

function calculatePreview(settings: Record<string, number>) {
  const assumedPrice = 500;
  const capitalPerTrade = Number(settings.capital_per_trade || 0);
  const marginMultiplier = Number(settings.margin_multiplier || 0);
  const maxTrades = Number(settings.max_trades_per_day || 0);
  return {
    positionSize: Math.floor((capitalPerTrade * (marginMultiplier || 1)) / assumedPrice),
    effectiveCapital: capitalPerTrade * marginMultiplier,
    maxDailyRisk: capitalPerTrade * Number(settings.sl_pct || 0) / 100 * maxTrades,
    maxDailyReward: capitalPerTrade * Number(settings.target_pct || 0) / 100 * maxTrades,
  };
}

function formatMoney(value: number) {
  return `Rs ${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function roundMoney(value: number) {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

function formatInputMoney(value: unknown) {
  const amount = Number(value);
  return Number.isFinite(amount) ? roundMoney(amount).toFixed(2) : '0.00';
}

function roundForField(key: string, value: number) {
  const integerFields = new Set(['max_trades_per_day', 'max_buy_trades', 'max_sell_trades', 'supertrend_period', 'min_volume', 'silver_lots', 'silver_breakout_points', 'sl_points', 'target_points', 'tsl_trigger_points', 'tsl_distance_points']);
  const rupeeFields = new Set(['starting_capital', 'capital_per_trade', 'min_total_value', 'ltp_min', 'ltp_max']);
  if (integerFields.has(key)) return Math.round(value);
  if (rupeeFields.has(key)) return roundMoney(value);
  return Math.round((value + Number.EPSILON) * 10_000) / 10_000;
}
