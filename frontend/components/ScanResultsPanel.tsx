'use client';
import { useMemo, useState } from 'react';

const PAGE_SIZE = 20;
const COLUMNS = ['symbol', 'side', 'gap_pct', 'vwap', 'rsi', 'adx', 'supertrend', 'volume', 'selected_for_trade', 'rejection_reason'];
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

export default function ScanResultsPanel({ results }: { results: any }) {
  const [query, setQuery] = useState('');
  const [sortKey, setSortKey] = useState('gap_pct');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [page, setPage] = useState(0);
  const rows = results?.passed_opening_range || [];
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows
      .filter((row: any) => !q || String(row.symbol || '').toLowerCase().includes(q))
      .sort((a: any, b: any) => {
        const av = cellValue(a, sortKey);
        const bv = cellValue(b, sortKey);
        if (typeof av === 'number' && typeof bv === 'number') return sortDir === 'asc' ? av - bv : bv - av;
        return sortDir === 'asc'
          ? String(av).localeCompare(String(bv))
          : String(bv).localeCompare(String(av));
      });
  }, [rows, query, sortKey, sortDir]);

  if (!results || results.message) {
    return (
      <section className="rounded border border-[#1f2937] bg-[#111827] p-3 text-sm text-gray-500">
        {results?.message || 'Scan results will appear here at 9:16 AM when the market opens.'}
      </section>
    );
  }

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  const funnel = buildConditionFunnel(results, rows);

  function sort(column: string) {
    setPage(0);
    if (sortKey === column) setSortDir((dir) => dir === 'asc' ? 'desc' : 'asc');
    else {
      setSortKey(column);
      setSortDir(column === 'gap_pct' ? 'desc' : 'asc');
    }
  }

  return (
    <section className="rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="grid gap-2 text-xs sm:grid-cols-3 lg:grid-cols-6">
        <ScanStat label="Scanned" value={results.total_scanned} />
        <ScanStat label="Passed Gap Filter" value={rows.length} />
        <ScanStat label="Buy" value={results.buy_candidates} />
        <ScanStat label="Sell" value={results.sell_candidates} />
        <ScanStat label="Selected" value={(results.buy_selected || 0) + (results.sell_selected || 0)} />
        <ScanStat label="Filtered Out" value={results.total_filtered_out} />
      </div>
      <div className="mt-2 text-xs text-gray-500">Last scan: {formatTime(results.scan_time)}</div>

      <div className="mt-4 rounded border border-[#1f2937] bg-[#0d1117] p-3">
        <div className="label">Condition Funnel</div>
        <p className="mt-1 text-xs text-gray-500">
          Temporary screener check: how many stocks survived each condition, step by step.
        </p>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {funnel.map((step, index) => (
            <FunnelStep key={`${step.label}-${index}`} step={step} index={index} />
          ))}
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <h3 className="label">All Candidates</h3>
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
                <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
                <td className={`table-cell font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>{row.side}</td>
                <td className="table-cell num text-gray-100">{formatNumber(row.gap_pct)}%</td>
                {['vwap', 'rsi', 'adx', 'supertrend', 'volume'].map((name) => (
                  <IndicatorCell key={name} result={row.indicator_results?.[name]} />
                ))}
                <td className="table-cell text-gray-100">{row.selected_for_trade ? 'Yes' : 'No'}</td>
                <td className="table-cell text-gray-500">{row.rejection_reason || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-center justify-between text-xs text-gray-500">
        <span>Page {page + 1} / {pageCount}</span>
        <div className="flex gap-2">
          <button disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))} className="rounded border border-[#1f2937] px-2 py-1 disabled:opacity-40">Previous</button>
          <button disabled={page >= pageCount - 1} onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))} className="rounded border border-[#1f2937] px-2 py-1 disabled:opacity-40">Next</button>
        </div>
      </div>
    </section>
  );
}

function ScanStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#0d1117] p-2">
      <div className="label">{label}</div>
      <div className="num mt-1 text-lg font-semibold text-gray-100">{Number(value || 0).toLocaleString('en-IN')}</div>
    </div>
  );
}

function buildConditionFunnel(results: any, rows: any[]) {
  const steps: { label: string; passed: number; total: number; note?: string }[] = [];
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
}: {
  step: { label: string; passed: number; total: number; note?: string };
  index: number;
}) {
  const pct = step.total > 0 ? step.passed / step.total * 100 : 0;
  return (
    <div className="rounded border border-[#1f2937] bg-[#111827] p-2">
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
    </div>
  );
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
