'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const FIELDS: [string, string][] = [
  ['brokerage_flat', 'Brokerage - flat Rs per order'],
  ['brokerage_pct', 'Brokerage - % of turnover (whichever lower applies)'],
  ['stt_pct', 'STT - % on sell turnover'],
  ['exchange_pct', 'Exchange Charges - % on turnover'],
  ['sebi_pct', 'SEBI Charges - % on turnover'],
  ['gst_pct', 'GST - % on (brokerage + exchange + SEBI)'],
  ['stamp_duty_pct', 'Stamp Duty - % on buy turnover'],
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

  if (!config) return <p>Loading charges config...</p>;

  return (
    <section className="max-w-xl">
      <h2 className="text-xl font-semibold text-white">Charges Settings</h2>
      {error && <p className="mt-3 text-sm text-danger">{error}</p>}
      <p className="mt-3 text-sm text-textSoft">
        These rates feed the Net P&amp;L calculation on every closed trade, for both algos.
        Cross-check against a current Fyers contract note periodically - exchange/regulatory
        rates do get revised.
      </p>
      {FIELDS.map(([key, label]) => (
        <div key={key} className="mt-4">
          <label className="mb-1 block text-sm text-textSoft">{label}</label>
          <input
            type="number" step="0.0001" value={config[key]}
            onChange={(e) => setConfig({ ...config, [key]: parseFloat(e.target.value) })}
            className="control"
          />
        </div>
      ))}
      <button onClick={save} className="mt-5 rounded-md bg-action px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-success hover:text-ink">
        {saved ? 'Saved' : 'Save changes'}
      </button>
    </section>
  );
}
