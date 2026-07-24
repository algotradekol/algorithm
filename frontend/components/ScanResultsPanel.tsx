'use client';
import { useEffect, useMemo, useState } from 'react';
import { PAGE_SIZE, PaginationControls } from './PaginationControls';
import { api } from '../lib/api';

const COLUMNS = ['rank', 'composite_score', 'symbol', 'sector', 'side', 'open', 'high', 'low', 'prev_close', 'gap_pct', 'vwap', 'rsi', 'adx', 'supertrend', 'volume', 'selected_for_trade', 'rejection_reason', 'manual_action'];
const FUNNEL_INDICATORS = [
  ['vwap', 'VWAP condition'],
  ['rsi', 'RSI / move condition'],
  ['adx', 'ADX / strength condition'],
  ['supertrend', 'Supertrend condition'],
  ['ema20', 'EMA20 condition'],
  ['ema50', 'EMA50 condition'],
  ['volume', 'Volume condition'],
  ['liquidity', 'Liquidity condition'],
  ['price_range', 'Price range condition'],
] as const;

type FunnelStepData = {
  label: string;
  passed: number;
  total: number;
  note?: string;
};

type ScanFilter = 'all' | 'passed' | 'buy' | 'sell' | 'selected' | 'filtered';

export default function ScanResultsPanel({ results, algoId, onRefresh }: { results: any; algoId?: string; onRefresh?: () => void }) {
  const [query, setQuery] = useState('');
  const [sortKey, setSortKey] = useState('gap_pct');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [page, setPage] = useState(0);
  const [scanFilter, setScanFilter] = useState<ScanFilter>('all');
  const [funnelFilter, setFunnelFilter] = useState<number | null>(null);
  const [busyTrade, setBusyTrade] = useState<string | null>(null);
  const [tradeError, setTradeError] = useState('');
  const rows = results?.passed_opening_range || [];

  useEffect(() => {
    setScanFilter('all');
    setFunnelFilter(null);
    setPage(0);
  }, [results?.scan_time]);

  const funnel = buildConditionFunnel(results, rows);
  const bestMatches = [...rows]
    .filter((row: any) => Number.isFinite(Number(row.composite_score)))
    .sort((left: any, right: any) => Number(left.rank || Infinity) - Number(right.rank || Infinity))
    .slice(0, 4);
  const sectorBreakdown = Array.isArray(results?.sector_breakdown) ? results.sector_breakdown : [];
  const schedule = results?.schedule;
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows
      .filter((row: any) => matchesScanFilter(row, scanFilter))
      .filter((row: any) => funnelFilter === null || matchesFunnelStep(row, funnel, funnelFilter))
      .filter((row: any) => !q || String(row.symbol || '').toLowerCase().includes(q))
      .sort((a: any, b: any) => {
        const av = cellValue(a, sortKey);
        const bv = cellValue(b, sortKey);
        if (typeof av === 'number' && typeof bv === 'number') return sortDir === 'asc' ? av - bv : bv - av;
        return sortDir === 'asc'
          ? String(av).localeCompare(String(bv))
          : String(bv).localeCompare(String(av));
      });
  }, [rows, query, scanFilter, funnelFilter, sortKey, sortDir, funnel]);

  if (!results || results.message) {
    return (
      <section className="rounded border border-[#1f2937] bg-[#111827] p-3 text-sm text-gray-500">
        {schedule?.enabled && <TestScheduleStatus schedule={schedule} />}
        <div>{results?.message || 'Scan results will appear here at 9:16 AM when the market opens.'}</div>
        {schedule?.enabled && (
          <p className="mt-2 text-xs text-[#93c5fd]">
            Scheduled test is turned on, so the scanner is waiting for the configured candle window instead of the default 09:15-09:17 opening range.
          </p>
        )}
      </section>
    );
  }

  const visible = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  function sort(column: string) {
    setPage(0);
    if (sortKey === column) setSortDir((dir) => dir === 'asc' ? 'desc' : 'asc');
    else {
      setSortKey(column);
      setSortDir(column === 'gap_pct' ? 'desc' : 'asc');
    }
  }

  function selectFilter(filter: ScanFilter) {
    setScanFilter(filter);
    setFunnelFilter(null);
    setPage(0);
  }

  function selectFunnelStep(index: number) {
    setFunnelFilter((current) => current === index ? null : index);
    setScanFilter('all');
    setPage(0);
  }

  async function manualTrade(row: any, side: 'BUY' | 'SELL') {
    if (!algoId) {
      setTradeError('Manual trading is unavailable for this panel.');
      return;
    }
    const symbol = String(row.symbol || '').trim();
    if (!symbol) return;
    const price = Number(row.ltp ?? row.close ?? row.open);
    if (!Number.isFinite(price) || price <= 0) {
      setTradeError(`No usable price is available for ${symbol}.`);
      return;
    }
    setTradeError('');
    setBusyTrade(`${symbol}:${side}`);
    try {
      await api.manualTrade(algoId, {
        symbol,
        side,
        price,
        trigger: `Manual ${side} from scan panel at rank ${row.rank || 'n/a'}`,
      });
      onRefresh?.();
    } catch (error: any) {
      setTradeError(error?.message || 'Could not place the manual trade.');
    } finally {
      setBusyTrade(null);
    }
  }

  return (
    <section className="rounded border border-[#1f2937] bg-[#111827] p-3">
      {results.schedule?.enabled && <TestScheduleStatus schedule={results.schedule} />}
      {results.scan_status && results.scan_status !== 'complete' && (
        <div className="mb-3 rounded border border-[#f59e0b]/50 bg-[#f59e0b]/10 px-3 py-2 text-xs text-[#fbbf24]">
          <i className="ri-error-warning-fill mr-1" />
          {results.scan_message || 'Opening market data was incomplete. This is not a valid zero-candidate scan.'}
        </div>
      )}
      <div className="grid gap-2 text-xs sm:grid-cols-3 lg:grid-cols-6">
        <ScanStat label="Scanned" value={results.total_scanned} filter="all" active={scanFilter === 'all'} onClick={() => selectFilter('all')} />
        <ScanStat label="Passed Gap Filter" value={rows.filter((row: any) => row.gap_passed === true || row.opening_range_gap_passed === true).length} filter="passed" active={scanFilter === 'passed'} onClick={() => selectFilter('passed')} />
        <ScanStat label="Buy" value={results.buy_candidates} filter="buy" active={scanFilter === 'buy'} onClick={() => selectFilter('buy')} />
        <ScanStat label="Sell" value={results.sell_candidates} filter="sell" active={scanFilter === 'sell'} onClick={() => selectFilter('sell')} />
        <ScanStat label="Selected" value={rows.filter((row: any) => row.selected_for_trade).length} filter="selected" active={scanFilter === 'selected'} onClick={() => selectFilter('selected')} />
        <ScanStat label="Filtered Out" value={results.total_filtered_out} filter="filtered" active={scanFilter === 'filtered'} onClick={() => selectFilter('filtered')} />
      </div>
      <div className="mt-2 text-xs text-gray-500">Last scan: {formatTime(results.scan_time)}</div>

      {bestMatches.length > 0 && <div className="mt-4 rounded border border-[#3b82f6]/30 bg-[#0d1117] p-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2"><div className="label">Best Matches</div><p className="text-[11px] text-gray-500">{results.ranking?.method || 'Highest composite score is selected first within your configured trade limits.'}</p></div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{bestMatches.map((row: any) => <BestMatchCard key={row.symbol} row={row} />)}</div>
      </div>}

      {sectorBreakdown.length > 0 && <div className="mt-4 rounded border border-[#1f2937] bg-[#0d1117] p-3">
        <div className="label">Sector Breakdown</div>
        <p className="mt-1 text-xs text-gray-500">We keep the current ranking, but add a sector context bonus so strong symbols can be viewed alongside their sector trend.</p>
        <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {sectorBreakdown.map((sector: any) => (
            <div key={sector.sector} className="rounded border border-[#1f2937] bg-[#111827] p-2">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-semibold text-gray-100">{sector.sector}</div>
                  <div className="text-[11px] text-gray-500">{sector.direction} sector · {sector.rows} symbols</div>
                </div>
                <div className="num text-right text-sm font-semibold text-gray-100">{formatScore(sector.avg_score)}</div>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-sm bg-[#020617]">
                <div className="h-full bg-[#22c55e]" style={{ width: `${Math.max(4, Math.min(100, Number(sector.alignment_strength || 0) * 100))}%` }} />
              </div>
              <div className="mt-1 text-[11px] text-gray-500">
                {sector.buy} BUY · {sector.sell} SELL · {sector.selected} selected · avg move {formatNumber(sector.avg_move_pct)}%
              </div>
            </div>
          ))}
        </div>
      </div>}

      {tradeError && <div className="mt-4 rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-xs text-[#ef4444]">{tradeError}</div>}

      <div className="mt-4 rounded border border-[#1f2937] bg-[#0d1117] p-3">
        <div className="label">Condition Funnel</div>
        <p className="mt-1 text-xs text-gray-500">
          Temporary screener check: how many stocks survived each condition, step by step.
        </p>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {funnel.map((step, index) => (
            <FunnelStep key={`${step.label}-${index}`} step={step} index={index} active={funnelFilter === index} onClick={() => selectFunnelStep(index)} />
          ))}
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="label">{funnelFilter !== null ? `Passed: ${funnel[funnelFilter]?.label}` : scanFilter === 'all' ? 'All Candidates' : `${filterLabel(scanFilter)} Candidates`}</h3>
          {(scanFilter !== 'all' || funnelFilter !== null) && <button onClick={() => { setScanFilter('all'); setFunnelFilter(null); setPage(0); }} className="mt-1 text-xs text-[#60a5fa]">Show all candidates</button>}
        </div>
        <input
          value={query}
          onChange={(e) => { setQuery(e.target.value); setPage(0); }}
          placeholder="Filter symbols..."
          className="control max-w-xs"
        />
      </div>

      <div className="mt-3 overflow-x-auto rounded border border-[#1f2937]">
        <table className="w-full min-w-max border-collapse text-xs">
          <thead className="bg-[#111827]">
            <tr>
              {COLUMNS.map((column) => (
                <th key={column} className="table-cell label">
                  <button onClick={() => sort(column)}>{label(column)} {sortKey === column ? (sortDir === 'asc' ? '↑' : '↓') : ''}</button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row: any, index: number) => (
              <tr key={`${row.symbol}-${index}`} className={`${index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'} border-l-2 ${rowBorder(row)}`}>
                <td className="table-cell num font-semibold text-[#60a5fa]">{row.rank ? `#${row.rank}` : '-'}</td>
                <td className="table-cell num font-semibold text-gray-100">{formatScore(row.composite_score)}</td>
                <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
                <td className="table-cell text-gray-400">{row.sector || '-'}</td>
                <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>{row.side}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.open)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.high)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.low)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.prev_close)}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.gap_pct)}%</td>
                {['vwap', 'rsi', 'adx', 'supertrend', 'volume'].map((name) => (
                  <IndicatorCell key={name} result={row.indicator_results?.[name]} />
                ))}
                <td className="table-cell text-gray-100">{row.selected_for_trade ? 'Yes' : 'No'}</td>
                <td className="table-cell text-gray-500">{row.rejection_reason || '-'}</td>
                <td className="table-cell">
                  <div className="flex flex-wrap gap-1.5">
                    <button
                      type="button"
                      onClick={() => manualTrade(row, 'BUY')}
                      disabled={busyTrade === `${row.symbol}:BUY`}
                      className="min-h-8 rounded border border-[#22c55e]/50 px-2 py-1 text-[11px] font-semibold text-[#22c55e] disabled:opacity-40"
                    >
                      BUY
                    </button>
                    <button
                      type="button"
                      onClick={() => manualTrade(row, 'SELL')}
                      disabled={busyTrade === `${row.symbol}:SELL`}
                      className="min-h-8 rounded border border-[#ef4444]/50 px-2 py-1 text-[11px] font-semibold text-[#ef4444] disabled:opacity-40"
                    >
                      SELL
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <PaginationControls page={page} totalRows={filtered.length} onPageChange={setPage} />
    </section>
  );
}

function TestScheduleStatus({ schedule }: { schedule: any }) {
  const messages: Record<string, string> = {
    waiting: `Scheduled test: waiting to collect the ${schedule.candle_time} IST candle. Entry evaluation starts at ${schedule.entry_time}.`,
    collecting_candle: `Scheduled test is active: collecting the ${schedule.candle_time} IST candle across the watchlist.`,
    evaluating_entries: `Scheduled test is evaluating the ${schedule.candle_time} candle and opening eligible paper positions.`,
    finished: `Scheduled test window ended. Review the scan funnel and positions below.`,
  };
  return <div className="mb-3 rounded border border-[#3b82f6]/50 bg-[#3b82f6]/10 px-3 py-2 text-xs text-[#93c5fd]"><i className="ri-time-fill mr-1" />{messages[schedule.state] || 'Scheduled test status is updating.'}</div>;
}

function BestMatchCard({ row }: { row: any }) {
  const components = row.score_breakdown || {};
  const componentText = Object.entries(components)
    .map(([name, value]) => `${name.replace(/_/g, ' ')} ${Math.round(Number(value) * 100)}%`)
    .join(' · ');
  return <div className={`border-l-2 border border-[#1f2937] bg-[#111827] p-2 ${row.side === 'BUY' ? 'border-l-[#22c55e]' : 'border-l-[#ef4444]'}`}>
    <div className="flex items-center justify-between gap-2"><span className="num text-[#60a5fa]">#{row.rank}</span><span className={`text-xs font-semibold ${row.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{row.side}</span></div>
    <div className="mt-1 truncate font-mono text-sm font-semibold text-gray-100">{row.symbol}</div>
    <div className="mt-1 truncate text-[10px] uppercase tracking-[0.18em] text-gray-500">{row.sector || 'Unclassified sector'}</div>
    <div className="num mt-1 text-lg font-semibold text-gray-100">{formatScore(row.composite_score)}</div>
    <div className="text-[10px] uppercase tracking-wide text-gray-500">Composite score / 100</div>
    <div className="mt-1 line-clamp-2 text-[10px] text-gray-500" title={componentText}>{componentText || 'Gap-strength ranking'}</div>
  </div>;
}

function ScanStat({
  label,
  value,
  filter,
  active,
  onClick,
}: {
  label: string;
  value: number;
  filter: ScanFilter;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={`Show ${filterLabel(filter).toLowerCase()} candidates`}
      onClick={onClick}
      className={`min-h-16 rounded border p-2 text-left transition-colors ${active ? 'border-[#3b82f6] bg-[#172554]' : 'border-[#1f2937] bg-[#0d1117] hover:border-[#3b82f6]/70'}`}
    >
      <div className="label">{label}</div>
      <div className="num mt-1 text-lg font-semibold text-gray-100">{Number(value || 0).toLocaleString('en-IN')}</div>
    </button>
  );
}

function matchesScanFilter(row: any, filter: ScanFilter) {
  if (filter === 'all') return true;
  if (filter === 'passed') return row.gap_passed === true || row.opening_range_gap_passed === true;
  if (filter === 'buy') return row.side === 'BUY';
  if (filter === 'sell') return row.side === 'SELL';
  if (filter === 'selected') return Boolean(row.selected_for_trade);
  return Boolean(row.rejection_reason);
}

function filterLabel(filter: ScanFilter) {
  const labels: Record<ScanFilter, string> = {
    all: 'All',
    passed: 'Passed Gap Filter',
    buy: 'BUY',
    sell: 'SELL',
    selected: 'Selected',
    filtered: 'Filtered Out',
  };
  return labels[filter];
}

function buildConditionFunnel(results: any, rows: any[]) {
  if (Array.isArray(results?.condition_breakdown) && results.condition_breakdown.length) {
    return results.condition_breakdown as FunnelStepData[];
  }

  const steps: FunnelStepData[] = [];
  const totalScanned = Number(results?.total_scanned || rows.length || 0);
  const signalPassed = rows.length;
  steps.push({ label: 'Scanned universe', passed: totalScanned, total: totalScanned });
  steps.push({
    label: 'Condition 1: signal candle',
    passed: signalPassed,
    total: totalScanned,
    note: 'Opening-range signal rule',
  });

  let survivors = rows;
  for (const [key, labelText] of FUNNEL_INDICATORS) {
    const enabledRows = survivors.filter((row: any) => row.indicator_results?.[key]?.enabled);
    if (!enabledRows.length) continue;
    const passedRows = survivors.filter((row: any) => {
      const result = row.indicator_results?.[key];
      return !result?.enabled || Boolean(result.passed);
    });
    steps.push({
      label: `Condition ${steps.length}: ${labelText}`,
      passed: passedRows.length,
      total: survivors.length,
    });
    survivors = passedRows;
  }

  const selected = rows.filter((row: any) => row.selected_for_trade).length
    || Number(results?.buy_selected || 0) + Number(results?.sell_selected || 0);
  steps.push({ label: 'Final: selected for trade', passed: selected, total: survivors.length });
  return steps;
}

function FunnelStep({
  step,
  index,
  active,
  onClick,
}: {
  step: FunnelStepData;
  index: number;
  active: boolean;
  onClick: () => void;
}) {
  const pct = step.total > 0 ? step.passed / step.total * 100 : 0;
  return (
    <button type="button" onClick={onClick} className={`rounded border p-2 text-left transition-colors ${active ? 'border-[#3b82f6] bg-[#172554]' : 'border-[#1f2937] bg-[#111827] hover:border-[#3b82f6]/70'}`}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-[0.16em] text-gray-500">Step {index + 1}</div>
          <div className="mt-1 text-xs font-semibold text-gray-200">{step.label}</div>
        </div>
        <div className="num text-right text-sm font-bold text-gray-100">
          {Number(step.passed || 0).toLocaleString('en-IN')}
          <span className="text-gray-500"> / {Number(step.total || 0).toLocaleString('en-IN')}</span>
        </div>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-sm bg-[#020617]">
        <div className="h-full bg-[#3b82f6]" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-gray-500">
        <span>{formatNumber(pct)}% pass</span>
        {step.note && <span className="truncate">{step.note}</span>}
      </div>
    </button>
  );
}

function matchesFunnelStep(row: any, funnel: FunnelStepData[], stepIndex: number) {
  const step = funnel[stepIndex];
  const labelText = step?.label.toLowerCase() || '';
  if (labelText.includes('scanned universe')) return true;
  if (labelText.includes('candle received')) return row.candle_received === true;
  if (labelText.includes('open equals')) return row.shape_passed === true;
  if (labelText.includes('opening range + gap')) return row.opening_range_gap_passed === true || row.gap_passed === true;
  if (labelText.includes('gap')) return row.gap_passed === true;
  if (labelText.includes('selected for trade')) return Boolean(row.selected_for_trade);

  // Indicator steps are cumulative. Clicking RSI shows rows that passed the
  // opening rule, every earlier active indicator, and RSI itself.
  if (!(row.opening_range_gap_passed === true || row.gap_passed === true)) return false;
  let foundIndicator = false;
  for (let index = 1; index <= stepIndex; index += 1) {
    const indicatorKey = FUNNEL_INDICATORS.find(([key]) => funnel[index]?.label.toLowerCase().includes(key))?.[0];
    if (!indicatorKey) continue;
    foundIndicator = true;
    const indicator = row.indicator_results?.[indicatorKey];
    if (indicator?.enabled && !indicator.passed) return false;
  }
  return foundIndicator;
}

function IndicatorCell({ result }: { result: any }) {
  if (!result) return <td className="table-cell text-gray-500"><Dot tone="bg-gray-500" /> -</td>;
  return (
    <td className="table-cell num text-gray-100">
      <span className="inline-flex items-center gap-2">
        <Dot tone={!result.enabled ? 'bg-gray-500' : result.passed ? 'bg-[#22c55e]' : 'bg-[#ef4444]'} />
        {formatNumber(result.value)}
      </span>
    </td>
  );
}

function Dot({ tone }: { tone: string }) {
  return <span className={`inline-block h-2 w-2 rounded-full ${tone}`} />;
}

function rowBorder(row: any) {
  if (row.selected_for_trade) return 'border-[#22c55e]';
  if (row.rejection_reason === 'failed_indicator_filter') return 'border-[#ef4444]';
  return 'border-[#f59e0b]';
}

function cellValue(row: any, key: string) {
  if (row.indicator_results?.[key]) return Number(row.indicator_results[key].value || 0);
  return row[key] ?? '';
}

function label(column: string) {
  return column.replace(/_/g, ' ');
}

function formatNumber(value: any) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return number.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function formatScore(value: any) {
  const score = Number(value);
  return Number.isFinite(score) ? score.toFixed(2) : '-';
}

function formatTime(value: string) {
  if (!value) return '--';
  const normalized = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`;
  return new Date(normalized).toLocaleTimeString('en-IN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZone: 'Asia/Kolkata',
  });
}
