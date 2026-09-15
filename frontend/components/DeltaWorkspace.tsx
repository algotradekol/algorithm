'use client';

import { useEffect, useState } from 'react';
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
const DELTA_ASSET_STORAGE_KEY = 'delta.selectedAsset';
const deltaTabStorageKey = (asset: DeltaAsset) => `delta.${asset}.selectedTab`;

function readStoredValue(key: string, fallback: string) {
  if (typeof window === 'undefined') return fallback;
  return window.localStorage.getItem(key) || fallback;
}

export default function DeltaWorkspace({ capabilities }: { capabilities: DeltaCapabilities | null }) {
  const [selected, setSelected] = useState<DeltaAsset>(() => readStoredValue(DELTA_ASSET_STORAGE_KEY, 'gold') as DeltaAsset);
  if (!capabilities) return <p className="panel p-4 text-sm text-gray-400">Loading Delta configuration...</p>;
  if (capabilities.config_error) return <p role="alert" className="panel p-4 text-sm text-[#f87171]">{capabilities.config_error}</p>;
  const groups = capabilities.assets || {
    gold: { enabled: capabilities.delta_enabled, enabled_timeframes: capabilities.enabled_timeframes, sections: capabilities.sections },
    silver: { enabled: false, enabled_timeframes: [], sections: { overview: false, activity: false, backtest: false } },
  };
  const available = (['gold', 'silver'] as const).filter(key => capabilities.delta_enabled && groups[key].enabled);
  const asset = available.includes(selected) ? selected : available[0];
  if (!asset) return <p className="panel p-4 text-sm text-gray-400">No Delta sections are enabled.</p>;
  function selectAsset(next: DeltaAsset) {
    setSelected(next);
    window.localStorage.setItem(DELTA_ASSET_STORAGE_KEY, next);
  }
  return <section>
    <nav aria-label="Delta metals" className="mb-2 flex gap-2">
      {available.map(key => <button key={key} aria-pressed={asset === key} onClick={() => selectAsset(key)} className={`rounded-md border px-4 py-2 text-left ${asset === key ? (key === 'gold' ? 'border-[#eab308]/60 bg-[#eab308]/10 text-[#fde68a]' : 'border-[#94a3b8] bg-[#94a3b8]/10 text-gray-100') : 'border-[#1f2937] text-gray-500 hover:text-gray-200'}`}>
        <span className="text-sm font-semibold">{key === 'gold' ? 'Gold' : 'Silver'}</span>
        <span className="ml-2 text-[11px] opacity-70">{groups[key].enabled_timeframes.length} TF</span>
      </button>)}
    </nav>
    <MetalWorkspace key={asset} asset={asset} group={groups[asset]} />
  </section>;
}

function MetalWorkspace({ asset, group }: { asset: DeltaAsset; group: Group }) {
  const [selected, setSelected] = useState(() => readStoredValue(deltaTabStorageKey(asset), 'overview'));
  const [mode, setMode] = useState<'paper' | 'live'>('paper');
  const [liveConfirmOpen, setLiveConfirmOpen] = useState(false);
  const [liveConfirmed, setLiveConfirmed] = useState(false);
  const metal = asset === 'gold' ? 'Gold' : 'Silver';
  const effectiveMode = asset === 'gold' ? mode : 'paper';
  function chooseMode(nextMode: 'paper' | 'live') {
    if (nextMode === 'paper') {
      setMode('paper');
      setLiveConfirmOpen(false);
      setLiveConfirmed(false);
      return;
    }
    if (mode !== 'live') {
      setLiveConfirmOpen(true);
      setLiveConfirmed(false);
    }
  }
  function confirmLiveMode() {
    if (!liveConfirmed) return;
    setMode('live');
    setLiveConfirmOpen(false);
    setLiveConfirmed(false);
  }
  const tabs = [
    ...(group.sections.overview ? [{ key: 'overview', label: `${metal} dashboard` }] : []),
    ...group.enabled_timeframes.map(minutes => ({ key: String(minutes), label: deltaTimeframeLabel(minutes) })),
    ...(group.sections.activity ? [{ key: 'activity', label: 'Positions & Orders' }] : []),
    ...(group.sections.backtest ? [{ key: 'backtest', label: `${metal} backtest` }] : []),
  ];
  const tab = tabs.some(t => t.key === selected) ? selected : tabs[0]?.key;
  useEffect(() => {
    if (tab && tab !== selected) {
      setSelected(tab);
      window.localStorage.setItem(deltaTabStorageKey(asset), tab);
    }
  }, [asset, selected, tab]);
  function selectTab(next: string) {
    setSelected(next);
    window.localStorage.setItem(deltaTabStorageKey(asset), next);
  }
  return <>
    <div className="mb-3 flex flex-wrap items-end justify-between gap-2 border-b border-[#1f2937]">
      <nav aria-label={`Delta ${metal} timeframes`} className="flex gap-5 overflow-x-auto whitespace-nowrap">
        {tabs.map(item => <button key={item.key} aria-pressed={tab === item.key} onClick={() => selectTab(item.key)} className={`min-h-8 border-b-2 py-2 text-sm ${tab === item.key ? 'border-[#3b82f6] text-gray-100' : 'border-transparent text-gray-500 hover:text-gray-300'}`}>{item.label}</button>)}
      </nav>
      {asset === 'gold' && <div className="mb-1 flex rounded-md border border-[#334155] bg-[#0a0e14] p-0.5">
        {(['paper', 'live'] as const).map(value => <button key={value} aria-pressed={mode === value} onClick={() => chooseMode(value)} className={`rounded px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide ${mode === value ? (value === 'live' ? 'bg-[#ef4444]/20 text-[#f87171]' : 'bg-[#3b82f6]/20 text-[#93c5fd]') : 'text-gray-500 hover:text-gray-200'}`}>{value}</button>)}
      </div>}
    </div>
    {liveConfirmOpen && <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[#ef4444]/50 bg-[#ef4444]/10 p-3 text-sm text-[#fecaca]">
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={liveConfirmed} onChange={event => setLiveConfirmed(event.target.checked)} />
        I confirm I want to switch Delta Gold from paper to live controls.
      </label>
      <div className="flex gap-2">
        <button className="rounded border border-[#334155] px-3 py-1.5 text-xs text-gray-200" type="button" onClick={() => { setLiveConfirmOpen(false); setLiveConfirmed(false); }}>Cancel</button>
        <button className="rounded border border-[#ef4444] bg-[#ef4444]/20 px-3 py-1.5 text-xs font-semibold text-[#fecaca] disabled:opacity-40" type="button" disabled={!liveConfirmed} onClick={confirmLiveMode}>Enter live mode</button>
      </div>
    </div>}
    <div key={`${asset}-${tab}`}>
      {tab === 'overview' ? <DeltaOverviewTab asset={asset} mode={effectiveMode} />
        : tab === 'activity' ? <DeltaActivityTab asset={asset} enabledTimeframes={group.enabled_timeframes} />
        : tab === 'backtest' ? <DeltaBacktestTab asset={asset} enabledTimeframes={group.enabled_timeframes} />
        : tab ? <DeltaTab asset={asset} mode={effectiveMode} minutes={Number(tab)} />
        : <p className="panel p-4 text-sm text-gray-400">No {metal} sections are enabled.</p>}
    </div>
  </>;
}
