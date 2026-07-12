'use client';
import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { supabase } from '../../lib/supabaseClient';
import AlgoTab from '../../components/AlgoTab';
import CompareTab from '../../components/CompareTab';
import ChargesPanel from '../../components/ChargesPanel';
import HistoryTab from '../../components/HistoryTab';

const TABS = ['Algo 1', 'Algo 2', 'Compare', 'History', 'Charges'] as const;

export default function Dashboard() {
  const [tab, setTab] = useState<(typeof TABS)[number]>('Algo 1');
  const [ready, setReady] = useState(false);
  const router = useRouter();

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (!session) router.replace('/login');
      else setReady(true);
    });
  }, [router]);

  if (!ready) return null;

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h2>Algo Paper Trading</h2>
        <button
          onClick={async () => { await supabase.auth.signOut(); router.replace('/login'); }}
          style={{ background: 'none', border: '1px solid #333', color: '#8a94a3', padding: '6px 12px', borderRadius: 6 }}
        >
          Log out
        </button>
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
        {TABS.map((t) => (
          <button
            key={t} onClick={() => setTab(t)}
            style={{
              padding: '8px 16px', borderRadius: 8, border: 'none', cursor: 'pointer',
              background: tab === t ? '#2a78d6' : '#151b23',
              color: tab === t ? '#fff' : '#8a94a3',
            }}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'Algo 1' && <AlgoTab algoId="algo1" displayName="Algo 1 - Opening Range Gap" />}
      {tab === 'Algo 2' && <AlgoTab algoId="algo2" displayName="Algo 2 - VWAP/EMA/Volume Momentum" />}
      {tab === 'Compare' && <CompareTab />}
      {tab === 'History' && <HistoryTab />}
      {tab === 'Charges' && <ChargesPanel />}
    </div>
  );
}
