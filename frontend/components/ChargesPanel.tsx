'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const FIELDS: [string, string, string][] = [
  ['brokerage_flat', 'Brokerage flat', 'Rs per executed order, per leg'],
  ['brokerage_pct', 'Brokerage percent', 'applied to turnover, capped by flat brokerage'],
  ['stt_pct', 'STT', 'applied to sell-side turnover only'],
  ['exchange_pct', 'Exchange charges', 'applied to buy + sell turnover'],
  ['sebi_pct', 'SEBI charges', 'applied to buy + sell turnover'],
  ['gst_pct', 'GST', 'applied to brokerage + exchange + SEBI charges'],
  ['stamp_duty_pct', 'Stamp duty', 'applied to buy-side turnover only'],
];

export default function ChargesPanel() {
  const [config, setConfig] = useState<Record<string, number> | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api.getCharges().then((result) => {
      setConfig(result);
      setError('');
    }).catch((e: any) => {
      setError(e?.message || 'Failed to load charges config');
      console.error(e);
    });
  }, []);

  async function save() {
    if (!config) return;
    try {
      await api.updateCharges(config);
      setSaved(true);
      setError('');
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e?.message || 'Failed to save charges config');
      console.error(e);
    }
  }

  if (!config) return <p className="text-sm text-gray-500">Loading charges config...</p>;

  const preview = calculatePreview(config);

  return (
    <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(360px,0.8fr)]">
      <div className="panel p-4">
        <h2 className="text-base font-semibold text-gray-100">Charges Settings</h2>
        <p className="mt-2 text-xs text-gray-500">
          These rates feed Net P&L calculation on every closed paper trade. Cross-check against recent Fyers charges periodically.
        </p>
        {error && <p className="mt-3 rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}

        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {FIELDS.map(([key, label, helper]) => (
            <label key={key}>
              <div className="label">{label}</div>
              <input
                type="number"
                step="0.0001"
                value={Number.isFinite(config[key]) ? config[key] : 0}
                onChange={(e) => setConfig({ ...config, [key]: parseFloat(e.target.value) || 0 })}
                className="control mt-1 num"
              />
              <div className="mt-1 text-xs text-gray-500">{helper}</div>
            </label>
          ))}
        </div>

        <button
          onClick={save}
          className="mt-5 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2.5 text-sm font-semibold text-white"
        >
          {saved ? 'Saved' : 'Save changes'}
        </button>
      </div>

      <aside className="panel p-4">
        <h3 className="text-base font-semibold text-gray-100">Sample Trade Preview (Rs 50,000)</h3>
        <p className="mt-2 text-xs text-gray-500">Buy Rs 50,000 and sell Rs 51,000, 2% gross profit scenario.</p>

        <div className="mt-4 divide-y divide-[#1f2937] border-y border-[#1f2937] text-sm">
          <PreviewRow label="Brokerage" value={preview.brokerage} />
          <PreviewRow label="STT" value={preview.stt} />
          <PreviewRow label="Exchange" value={preview.exchange_charges} />
          <PreviewRow label="SEBI" value={preview.sebi_charges} />
          <PreviewRow label="GST" value={preview.gst} />
          <PreviewRow label="Stamp Duty" value={preview.stamp_duty} />
          <PreviewRow label="Total Charges" value={preview.total_charges} strong />
          <PreviewRow label="Net P&L" value={preview.net_pnl} pnl strong />
        </div>
      </aside>
    </section>
  );
}

function PreviewRow({ label, value, strong, pnl }: { label: string; value: number; strong?: boolean; pnl?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <span className="text-xs uppercase tracking-wider text-gray-500">{label}</span>
      <span className={`num ${strong ? 'text-base font-semibold' : 'text-sm'} ${pnl ? pnlColor(value) : 'text-gray-100'}`}>
        {formatMoney(value)}
      </span>
    </div>
  );
}

function calculatePreview(config: Record<string, number>) {
  const buyValue = 50_000;
  const sellValue = 51_000;
  const turnover = buyValue + sellValue;
  const brokerageBuy = Math.min(config.brokerage_flat, config.brokerage_pct / 100 * buyValue);
  const brokerageSell = Math.min(config.brokerage_flat, config.brokerage_pct / 100 * sellValue);
  const brokerage = brokerageBuy + brokerageSell;
  const stt = config.stt_pct / 100 * sellValue;
  const exchangeCharges = config.exchange_pct / 100 * turnover;
  const sebiCharges = config.sebi_pct / 100 * turnover;
  const gst = config.gst_pct / 100 * (brokerage + exchangeCharges + sebiCharges);
  const stampDuty = config.stamp_duty_pct / 100 * buyValue;
  const totalCharges = brokerage + stt + exchangeCharges + sebiCharges + gst + stampDuty;
  const netPnl = (sellValue - buyValue) - totalCharges;

  return {
    brokerage: round2(brokerage),
    stt: round2(stt),
    exchange_charges: round2(exchangeCharges),
    sebi_charges: round2(sebiCharges),
    gst: round2(gst),
    stamp_duty: round2(stampDuty),
    total_charges: round2(totalCharges),
    net_pnl: round2(netPnl),
  };
}

function round2(value: number) {
  return Math.round(value * 100) / 100;
}

function formatMoney(value: number) {
  return `Rs ${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function pnlColor(value: number) {
  if (value > 0) return 'text-[#22c55e]';
  if (value < 0) return 'text-[#ef4444]';
  return 'text-gray-100';
}
