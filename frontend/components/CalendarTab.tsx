'use client';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';

export default function CalendarTab() {
  const [days, setDays] = useState<any[]>([]);
  const [selectedDate, setSelectedDate] = useState('');
  const [snapshots, setSnapshots] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [selectedSnapshot, setSelectedSnapshot] = useState<any | null>(null);
  const [error, setError] = useState('');

  async function loadDays() {
    setLoading(true);
    try {
      const result = await api.calendarDays(90);
      setDays(result);
      const firstDate = result?.[0]?.snapshot_date || todayIso();
      setSelectedDate((current) => current || firstDate);
      setError('');
    } catch (e: any) {
      setError(e?.message || 'Failed to load calendar');
    } finally {
      setLoading(false);
    }
  }

  async function saveToday() {
    setSaving(true);
    try {
      await api.saveCalendarSnapshot({ note: 'manual_dashboard_save' });
      await loadDays();
      setSelectedDate(todayIso());
      setError('');
    } catch (e: any) {
      setError(e?.message || 'Failed to save today snapshot');
    } finally {
      setSaving(false);
    }
  }

  async function deleteSelectedDate() {
    if (!selectedDate) return;
    if (!window.confirm(`Delete all calendar snapshots for ${selectedDate}?`)) return;
    try {
      await api.deleteCalendarDay(selectedDate);
      setSnapshots([]);
      setSelectedSnapshot(null);
      setSelectedDate('');
      await loadDays();
      setError('');
    } catch (e: any) {
      setError(e?.message || 'Failed to delete calendar date');
    }
  }

  async function deleteSnapshot(snapshot: any) {
    if (!window.confirm(`Delete ${snapshot.display_name || snapshot.algo_id} snapshot for ${snapshot.snapshot_date}?`)) return;
    try {
      await api.deleteCalendarSnapshot(snapshot.snapshot_date, snapshot.algo_id);
      setSnapshots((current) => current.filter((row) => row.algo_id !== snapshot.algo_id));
      setSelectedSnapshot(null);
      await loadDays();
      setError('');
    } catch (e: any) {
      setError(e?.message || 'Failed to delete algo snapshot');
    }
  }

  useEffect(() => {
    loadDays();
  }, []);

  useEffect(() => {
    if (!selectedDate) return;
    let cancelled = false;
    api.calendarDay(selectedDate)
      .then((result) => {
        if (!cancelled) {
          setSnapshots(result);
          setError('');
        }
      })
      .catch((e: any) => {
        if (!cancelled) setError(e?.message || 'Failed to load selected date');
      });
    return () => { cancelled = true; };
  }, [selectedDate]);

  const groupedDays = useMemo(() => {
    const map = new Map<string, any[]>();
    days.forEach((row) => {
      const key = row.snapshot_date;
      map.set(key, [...(map.get(key) || []), row]);
    });
    return Array.from(map.entries());
  }, [days]);

  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-100">Calendar</h2>
          <p className="mt-1 text-xs text-gray-500">
            Date-wise dashboard archive: summaries, positions, trades, scan funnel, settings, engine status, and Fyers status.
          </p>
        </div>
        <button
          onClick={saveToday}
          disabled={saving}
          className="inline-flex min-h-10 items-center justify-center gap-2 rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          <i className="ri-save-fill text-sm text-white" />
          {saving ? 'Saving...' : 'Save today snapshot'}
        </button>
      </div>

      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}

      <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="panel p-3">
          <div className="label mb-3">Trading Dates</div>
          {loading ? (
            <p className="text-sm text-gray-500">Loading calendar...</p>
          ) : groupedDays.length ? (
            <div className="space-y-2">
              {groupedDays.map(([date, rows]) => {
                const net = rows.reduce((sum, row) => sum + Number(row.summary?.realized_net_pnl || 0), 0);
                return (
                  <button
                    key={date}
                    onClick={() => setSelectedDate(date)}
                    className={`w-full rounded border p-3 text-left ${
                      selectedDate === date ? 'border-[#3b82f6] bg-[#3b82f6]/10' : 'border-[#1f2937] bg-[#0d1117]'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-sm font-semibold text-gray-100">{formatDate(date)}</span>
                      <span className={`num text-xs font-semibold ${pnlColor(net)}`}>{formatMoney(net)}</span>
                    </div>
                    <div className="mt-1 text-xs text-gray-500">{rows.length} algo snapshots</div>
                  </button>
                );
              })}
            </div>
          ) : (
            <p className="text-sm text-gray-500">No snapshots yet. Click Save today snapshot.</p>
          )}
        </aside>

        <div className="space-y-4">
          {selectedDate && snapshots.length > 0 && (
            <div className="flex justify-end">
              <button
                onClick={deleteSelectedDate}
                className="inline-flex min-h-10 items-center justify-center gap-2 rounded border border-[#ef4444]/60 bg-[#ef4444]/10 px-3 py-2 text-xs font-semibold text-[#ef4444]"
              >
                <i className="ri-delete-bin-fill text-sm text-[#ef4444]" />
                Delete whole date
              </button>
            </div>
          )}
          {!snapshots.length ? (
            <div className="panel p-4 text-sm text-gray-500">
              No saved snapshot for {selectedDate ? formatDate(selectedDate) : 'this date'} yet.
            </div>
          ) : snapshots.map((snapshot) => (
            <SnapshotCard
              key={`${snapshot.snapshot_date}-${snapshot.algo_id}`}
              snapshot={snapshot}
              onOpen={() => setSelectedSnapshot(snapshot)}
              onDelete={() => deleteSnapshot(snapshot)}
            />
          ))}
        </div>
      </div>

      {selectedSnapshot && (
        <SnapshotModal
          snapshot={selectedSnapshot}
          onClose={() => setSelectedSnapshot(null)}
          onDelete={() => deleteSnapshot(selectedSnapshot)}
        />
      )}
    </section>
  );
}

function SnapshotCard({ snapshot, onOpen, onDelete }: { snapshot: any; onOpen: () => void; onDelete: () => void }) {
  const summary = snapshot.summary || {};
  const positions = snapshot.positions || [];
  const trades = snapshot.trades || [];
  const scan = snapshot.scan_results || {};
  return (
    <article className="panel p-4">
      <div className="flex flex-col gap-2 border-b border-[#1f2937] pb-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-100">{snapshot.display_name || snapshot.algo_id}</h3>
          <p className="mt-1 text-xs text-gray-500">
            {formatDate(snapshot.snapshot_date)} · {snapshot.note || 'snapshot'} · updated {formatDateTime(snapshot.updated_at)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className={`num text-lg font-semibold ${pnlColor(Number(summary.realized_net_pnl || 0))}`}>
            {formatMoney(summary.realized_net_pnl)}
          </div>
          <button
            onClick={onOpen}
            className="inline-flex min-h-10 items-center gap-2 rounded border border-[#3b82f6] px-3 py-2 text-xs font-semibold text-[#3b82f6]"
          >
            <i className="ri-calendar-event-fill text-sm text-[#3b82f6]" />
            Open full data
          </button>
          <button
            onClick={onDelete}
            className="inline-flex min-h-10 items-center gap-2 rounded border border-[#ef4444]/60 bg-[#ef4444]/10 px-3 py-2 text-xs font-semibold text-[#ef4444]"
          >
            <i className="ri-delete-bin-fill text-sm text-[#ef4444]" />
            Delete
          </button>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-6">
        <MiniMetric label="Cash" value={formatMoney(summary.cash)} />
        <MiniMetric label="Trades" value={`${summary.trade_count_today || 0}`} />
        <MiniMetric label="Buy / Sell" value={`${summary.buy_count_today || 0}B ${summary.sell_count_today || 0}S`} />
        <MiniMetric label="Gross" value={formatMoney(summary.realized_gross_pnl)} />
        <MiniMetric label="Charges" value={formatMoney(summary.realized_charges)} />
        <MiniMetric label="Rows Saved" value={`${positions.length} pos / ${trades.length} trades`} />
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <ArchiveList title="Open Positions" rows={positions} empty="No open positions saved" />
        <ArchiveList title="Closed Trades" rows={trades} empty="No closed trades saved" />
        <div className="rounded border border-[#1f2937] bg-[#0d1117] p-3">
          <div className="label">Scan Funnel</div>
          {scan?.condition_breakdown?.length ? (
            <div className="mt-2 space-y-2">
              {scan.condition_breakdown.map((step: any, index: number) => (
                <div key={index} className="flex items-center justify-between gap-3 text-xs">
                  <span className="text-gray-500">{step.label}</span>
                  <span className="num text-gray-100">{step.passed} / {step.total}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No scan saved</p>
          )}
        </div>
      </div>
    </article>
  );
}

function SnapshotModal({ snapshot, onClose, onDelete }: { snapshot: any; onClose: () => void; onDelete: () => void }) {
  const positions = snapshot.positions || [];
  const trades = snapshot.trades || [];
  const scanRows = snapshot.scan_results?.passed_opening_range || [];
  return (
    <div className="fixed inset-0 z-50 bg-black/70 p-2 sm:p-4">
      <div className="mx-auto flex h-full max-w-[1500px] flex-col rounded border border-[#1f2937] bg-[#0a0e14] shadow-2xl">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[#1f2937] p-3">
          <div>
            <h3 className="text-base font-semibold text-gray-100">{snapshot.display_name || snapshot.algo_id}</h3>
            <p className="mt-1 text-xs text-gray-500">
              Full calendar archive for {formatDate(snapshot.snapshot_date)} · {positions.length} positions · {trades.length} trades · {scanRows.length} scan rows
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onDelete}
              className="inline-flex min-h-10 items-center gap-2 rounded border border-[#ef4444]/60 bg-[#ef4444]/10 px-3 py-2 text-xs font-semibold text-[#ef4444]"
            >
              <i className="ri-delete-bin-fill text-sm text-[#ef4444]" />
              Delete
            </button>
            <button
              onClick={onClose}
              className="inline-flex min-h-10 items-center gap-2 rounded border border-[#1f2937] px-3 py-2 text-xs font-semibold text-gray-300"
            >
              <i className="ri-close-circle-fill text-sm text-gray-400" />
              Close
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="grid gap-3">
            <FullTable
              title="Full Closed Trades"
              rows={trades}
              columns={[
                ['symbol', 'Symbol'],
                ['side', 'Side'],
                ['qty', 'Qty'],
                ['entry_price', 'Entry'],
                ['entry_time', 'Entry Time'],
                ['exit_price', 'Exit'],
                ['exit_time', 'Exit Time'],
                ['exit_reason', 'Reason'],
                ['entry_trigger', 'Trigger'],
                ['gross_pnl', 'Gross'],
                ['total_charges', 'Charges'],
                ['net_pnl', 'Net'],
              ]}
            />
            <FullTable
              title="Full Open Positions At Snapshot"
              rows={positions}
              columns={[
                ['symbol', 'Symbol'],
                ['side', 'Side'],
                ['qty', 'Qty'],
                ['entry_price', 'Entry'],
                ['ltp', 'LTP'],
                ['high_price', 'High'],
                ['low_price', 'Low'],
                ['sl_price', 'SL'],
                ['target_price', 'Target'],
                ['entry_trigger', 'Trigger'],
                ['unrealized_pnl', 'Unreal P&L'],
              ]}
            />
            <FullTable
              title="Full Scan Candidates"
              rows={scanRows}
              columns={[
                ['symbol', 'Symbol'],
                ['side', 'Side'],
                ['open', 'Open'],
                ['high', 'High'],
                ['low', 'Low'],
                ['prev_close', 'Prev Close'],
                ['gap_pct', 'Gap %'],
                ['selected_for_trade', 'Selected'],
                ['rejection_reason', 'Reason'],
              ]}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function FullTable({ title, rows, columns }: { title: string; rows: any[]; columns: [string, string][] }) {
  return (
    <section className="rounded border border-[#1f2937] bg-[#0d1117]">
      <div className="flex items-center justify-between gap-3 border-b border-[#1f2937] p-3">
        <div className="label">{title}</div>
        <div className="num text-xs text-gray-500">{rows.length} rows</div>
      </div>
      <div className="max-h-[55vh] overflow-auto">
        <table className="w-full min-w-[1100px] border-collapse text-xs">
          <thead className="sticky top-0 bg-[#111827]">
            <tr>
              {columns.map(([, label]) => <th key={label} className="table-cell label">{label}</th>)}
            </tr>
          </thead>
          <tbody>
            {!rows.length ? (
              <tr>
                <td colSpan={columns.length} className="table-cell text-gray-500">No rows saved</td>
              </tr>
            ) : rows.map((row, index) => (
              <tr key={row.id || `${row.symbol}-${index}`} className={index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
                {columns.map(([key]) => (
                  <td key={key} className={`table-cell ${key.includes('pnl') || key.includes('price') || key === 'ltp' || key === 'qty' ? 'num text-gray-100' : 'text-gray-300'}`}>
                    {formatCell(row[key], key)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#0d1117] p-2">
      <div className="label text-[10px]">{label}</div>
      <div className="num mt-1 text-sm font-semibold text-gray-100">{value}</div>
    </div>
  );
}

function ArchiveList({ title, rows, empty }: { title: string; rows: any[]; empty: string }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#0d1117] p-3">
      <div className="label">{title}</div>
      {rows.length ? (
        <div className="mt-2 max-h-72 space-y-2 overflow-y-auto pr-1">
          {rows.slice(0, 8).map((row, index) => (
            <div key={row.id || index} className="rounded border border-[#1f2937] bg-[#111827] p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs font-semibold text-gray-100">{row.symbol}</span>
                <span className={`text-xs font-semibold ${row.side === 'SELL' ? 'text-[#ef4444]' : 'text-[#22c55e]'}`}>
                  {row.side === 'SELL' ? 'S' : 'B'}
                </span>
              </div>
              <div className="num mt-1 text-xs text-gray-400">
                Entry {formatNumber(row.entry_price)} · Exit {row.exit_price ? formatNumber(row.exit_price) : 'open'}
              </div>
              <div className="mt-1 text-[11px] text-gray-500">
                Opened {formatTradeTime(row.entry_time)}{row.exit_time ? ` · Closed ${formatTradeTime(row.exit_time)}` : ''}
              </div>
              {row.entry_trigger && <div className="mt-1 text-[11px] text-gray-500">{row.entry_trigger}</div>}
            </div>
          ))}
          {rows.length > 8 && <div className="text-xs text-gray-500">+{rows.length - 8} more rows. Open full data to review all.</div>}
        </div>
      ) : (
        <p className="mt-2 text-sm text-gray-500">{empty}</p>
      )}
    </div>
  );
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function formatDate(value: string) {
  if (!value) return '--';
  return new Date(`${value}T00:00:00`).toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

function formatDateTime(value: string) {
  if (!value) return '--';
  return new Date(value).toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatTradeTime(value: unknown) {
  if (!value) return '--';
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  });
}

function formatMoney(value: unknown) {
  const number = Number(value || 0);
  return `Rs ${number.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function formatNumber(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return number.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function formatCell(value: unknown, key: string) {
  if (value === null || value === undefined || value === '') return '--';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (key === 'entry_time' || key === 'exit_time') return formatTradeTime(value);
  if (key.includes('price') || key.includes('pnl') || key === 'ltp' || key === 'open' || key === 'high' || key === 'low' || key === 'prev_close' || key === 'gap_pct' || key === 'qty') {
    return formatNumber(value);
  }
  return String(value);
}

function pnlColor(value: number) {
  if (value > 0) return 'text-[#22c55e]';
  if (value < 0) return 'text-[#ef4444]';
  return 'text-gray-100';
}
