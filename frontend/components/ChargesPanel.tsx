'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const FIELDS: [string, string][] = [
  ['brokerage_flat', 'Brokerage — flat ₹ per order'],
  ['brokerage_pct', 'Brokerage — % of turnover (whichever lower applies)'],
  ['stt_pct', 'STT — % on sell turnover'],
  ['exchange_pct', 'Exchange Charges — % on turnover'],
  ['sebi_pct', 'SEBI Charges — % on turnover'],
  ['gst_pct', 'GST — % on (brokerage + exchange + SEBI)'],
  ['stamp_duty_pct', 'Stamp Duty — % on buy turnover'],
];

export default function ChargesPanel() {
  const [config, setConfig] = useState<Record<string, number> | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { api.getCharges().then(setConfig); }, []);

  async function save() {
    if (!config) return;
    await api.updateCharges(config);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  if (!config) return <p>Loading charges config...</p>;

  return (
    <div style={{ maxWidth: 480 }}>
      <h3>Charges Settings</h3>
      <p style={{ color: '#8a94a3', fontSize: 13 }}>
        These rates feed the Net P&L calculation on every closed trade, for both algos.
        Cross-check against a current Fyers contract note periodically -- exchange/regulatory
        rates do get revised.
      </p>
      {FIELDS.map(([key, label]) => (
        <div key={key} style={{ marginBottom: 12 }}>
          <label style={{ display: 'block', fontSize: 13, color: '#8a94a3', marginBottom: 4 }}>{label}</label>
          <input
            type="number" step="0.0001" value={config[key]}
            onChange={(e) => setConfig({ ...config, [key]: parseFloat(e.target.value) })}
            style={{ width: '100%', padding: 8, borderRadius: 6, border: '1px solid #333', background: '#0b0f14', color: '#fff' }}
          />
        </div>
      ))}
      <button onClick={save} style={{ padding: '10px 20px', borderRadius: 6, background: '#2a78d6', color: '#fff', border: 'none' }}>
        {saved ? 'Saved ✓' : 'Save changes'}
      </button>
    </div>
  );
}
