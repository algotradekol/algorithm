'use client';

import { useState } from 'react';
import { DeltaAsset } from '../lib/api';
import DeltaTab from './DeltaTab';
import DeltaOverviewTab from './DeltaOverviewTab';
import DeltaActivityTab from './DeltaActivityTab';
import DeltaBacktestTab from './DeltaBacktestTab';

type Group = { enabled: boolean; enabled_timeframes: number[]; sections: { overview: boolean; activity: boolean; backtest: boolean } };
export type DeltaCapabilities = {
  delta_enabled: boolean; enabled_timeframes: number[]; sections: Group['sections'];
  assets?: Record<DeltaAsset, Group>; config_error?: string | null;
};

const deltaTimeframeLabel = (minutes: number) => (
  minutes === 60 ? '1 hr' : minutes === 120 ? '2 hr' : minutes === 240 ? '4 hr' : `${minutes} min`
);

export default function DeltaWorkspace({ capabilities }: { capabilities: DeltaCapabilities | null }) {
  const [selected, setSelected] = useState<DeltaAsset>('gold');
  if (!capabilities) return <p className="panel p-4 text-sm text-gray-400">Loading Delta configuration...</p>;
  if (capabilities.config_error) return <p role="alert" className="panel p-4 text-sm text-[#f87171]">{capabilities.config_error}</p>;
  const groups = capabilities.assets || {
    gold: { enabled: capabilities.delta_enabled, enabled_timeframes: capabilities.enabled_timeframes, sections: capabilities.sections },
    silver: { enabled: false, enabled_timeframes: [], sections: { overview: false, activity: false, backtest: false } },
  };
  const available = (['gold', 'silver'] as const).filter(key => capabilities.delta_enabled && groups[key].enabled);
  const asset = available.includes(selected) ? selected : available[0];
  if (!asset) return <p className="panel p-4 text-sm text-gray-400">No Delta sections are enabled.</p>;
  return <section>
    <nav aria-label="Delta metals" className="mb-4 flex gap-3">
      {available.map(key => <button key={key} aria-pressed={asset === key} onClick={() => setSelected(key)} className={`min-w-32 rounded-lg border px-6 py-3 text-left ${asset === key ? (key === 'gold' ? 'border-[#eab308]/60 bg-[#eab308]/10 text-[#fde68a]' : 'border-[#94a3b8] bg-[#94a3b8]/10 text-gray-100') : 'border-[#1f2937] text-gray-500 hover:text-gray-200'}`}>
        <span className="block text-base font-semibold">{key === 'gold' ? 'Gold' : 'Silver'}</span>
        <span className="mt-1 block text-[11px] opacity-70">{groups[key].enabled_timeframes.length} timeframes</span>
      </button>)}
    </nav>
    <MetalWorkspace key={asset} asset={asset} group={groups[asset]} />
  </section>;
}

function MetalWorkspace({ asset, group }: { asset: DeltaAsset; group: Group }) {
  const [selected, setSelected] = useState('overview');
  const metal = asset === 'gold' ? 'Gold' : 'Silver';
  const tabs = [
    ...(group.sections.overview ? [{ key: 'overview', label: `${metal} dashboard` }] : []),
    ...group.enabled_timeframes.map(minutes => ({ key: String(minutes), label: deltaTimeframeLabel(minutes) })),
    ...(group.sections.activity ? [{ key: 'activity', label: 'Positions & Orders' }] : []),
    ...(group.sections.backtest ? [{ key: 'backtest', label: `${metal} backtest` }] : []),
  ];
  const tab = tabs.some(t => t.key === selected) ? selected : tabs[0]?.key;
  return <>
    <nav aria-label={`Delta ${metal} timeframes`} className="mb-4 flex gap-6 overflow-x-auto whitespace-nowrap border-b border-[#1f2937]">
      {tabs.map(item => <button key={item.key} aria-pressed={tab === item.key} onClick={() => setSelected(item.key)} className={`min-h-10 border-b-2 py-3 text-sm ${tab === item.key ? 'border-[#3b82f6] text-gray-100' : 'border-transparent text-gray-500 hover:text-gray-300'}`}>{item.label}</button>)}
    </nav>
    <div key={`${asset}-${tab}`}>
      {tab === 'overview' ? <DeltaOverviewTab asset={asset} />
        : tab === 'activity' ? <DeltaActivityTab asset={asset} enabledTimeframes={group.enabled_timeframes} />
        : tab === 'backtest' ? <DeltaBacktestTab asset={asset} enabledTimeframes={group.enabled_timeframes} />
        : tab ? <DeltaTab asset={asset} minutes={Number(tab)} />
        : <p className="panel p-4 text-sm text-gray-400">No {metal} sections are enabled.</p>}
    </div>
  </>;
}
