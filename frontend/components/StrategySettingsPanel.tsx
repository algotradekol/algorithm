'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

type Field = [string, string, string];

const CAPITAL_FIELDS: Field[] = [
  ['starting_capital', 'Starting Capital (Rs)', 'baseline capital shown in strategy summary'],
  ['capital_per_trade', 'Capital Per Trade (Rs)', 'paper capital allocated to one new trade'],
  ['margin_multiplier', 'Margin Multiplier (x)', 'used for effective capital preview only'],
];

const RISK_FIELDS: Field[] = [
  ['target_pct', 'Target % (per trade)', 'profit target from entry price'],
  ['sl_pct', 'Stop Loss % (per trade)', 'stop loss from entry price'],
  ['max_trades_per_day', 'Max Trades Per Day', 'daily total trade cap'],
  ['max_buy_trades', 'Max Buy Trades Per Day', 'daily buy-side trade cap'],
  ['max_sell_trades', 'Max Sell Trades Per Day', 'daily sell-side trade cap'],
];

const INDICATOR_FIELDS: Field[] = [
  ['rsi_buy_threshold', 'RSI Buy Threshold', 'Algo 4 buy confirmation threshold'],
  ['rsi_sell_threshold', 'RSI Sell Threshold', 'Algo 4 sell confirmation threshold'],
  ['adx_threshold', 'ADX Threshold', 'minimum trend strength for Algo 4'],
  ['min_volume', 'Min Volume', 'minimum 9:15 candle volume'],
  ['min_total_value', 'Min Total Value (Rs)', 'minimum traded value for the day'],
  ['ltp_min', 'LTP Min (Rs)', 'minimum allowed entry price'],
  ['ltp_max', 'LTP Max (Rs)', 'maximum allowed entry price'],
  ['supertrend_period', 'Supertrend Period', 'ATR period used by Supertrend'],
  ['supertrend_multiplier', 'Supertrend Multiplier', 'ATR multiplier used by Supertrend'],
];

export default function StrategySettingsPanel({ algoId }: { algoId: string }) {
  const [settings, setSettings] = useState<Record<string, number> | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    api.getSettings(algoId).then((result) => {
      if (!cancelled) {
        setSettings(result);
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

  if (!settings) return <p className="text-sm text-gray-500">Loading strategy settings...</p>;

  const preview = calculatePreview(settings);

  return (
    <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(340px,0.8fr)]">
      <div className="panel p-4">
        <div className="mb-4 rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">
          Changes apply to new trades only. Open positions keep their original entry prices, SL, and targets.
        </div>
        {error && <p className="mb-4 rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}

        <FieldGroup title="Capital Settings" fields={CAPITAL_FIELDS} settings={settings} setSettings={setSettings} />
        <FieldGroup title="Risk Settings" fields={RISK_FIELDS} settings={settings} setSettings={setSettings} />
        {algoId === 'algo4' && <FieldGroup title="Indicator Thresholds" fields={INDICATOR_FIELDS} settings={settings} setSettings={setSettings} />}

        <button
          onClick={save}
          className="mt-5 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2.5 text-sm font-semibold text-white"
        >
          {saved ? 'Saved ✓' : 'Save settings'}
        </button>
      </div>

      <aside className="panel p-4">
        <h3 className="text-base font-semibold text-gray-100">Live Strategy Preview</h3>
        <p className="mt-2 text-xs text-gray-500">Uses assumed example price Rs 500.</p>
        <div className="mt-4 divide-y divide-[#1f2937] border-y border-[#1f2937] text-sm">
          <PreviewRow label="Position size" value={`${preview.positionSize.toLocaleString('en-IN')} qty`} />
          <PreviewRow label="Effective capital with margin" value={formatMoney(preview.effectiveCapital)} />
          <PreviewRow label="Max daily risk" value={formatMoney(preview.maxDailyRisk)} tone="text-[#ef4444]" />
          <PreviewRow label="Max daily reward" value={formatMoney(preview.maxDailyReward)} tone="text-[#22c55e]" />
        </div>
      </aside>
    </section>
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
  settings: Record<string, number>;
  setSettings: (settings: Record<string, number>) => void;
}) {
  return (
    <div className="mt-5 first:mt-0">
      <div className="label mb-3">{title}</div>
      <div className="grid gap-3 md:grid-cols-2">
        {fields.map(([key, label, helper]) => (
          <label key={key}>
            <div className="label">{label}</div>
            <input
              type="number"
              step="0.0001"
              value={Number.isFinite(settings[key]) ? settings[key] : 0}
              onChange={(e) => setSettings({ ...settings, [key]: parseFloat(e.target.value) || 0 })}
              className="control mt-1 num"
            />
            <div className="mt-1 text-xs text-gray-500">{helper}</div>
          </label>
        ))}
      </div>
    </div>
  );
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
    positionSize: Math.floor(capitalPerTrade / assumedPrice),
    effectiveCapital: capitalPerTrade * marginMultiplier,
    maxDailyRisk: capitalPerTrade * Number(settings.sl_pct || 0) / 100 * maxTrades,
    maxDailyReward: capitalPerTrade * Number(settings.target_pct || 0) / 100 * maxTrades,
  };
}

function formatMoney(value: number) {
  return `Rs ${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}
