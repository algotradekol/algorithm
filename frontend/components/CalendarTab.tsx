'use client';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';

export default function CalendarTab() {
  const [days, setDays] = useState<any[]>([]);
  const [selectedDate, setSelectedDate] = useState('');
  const [snapshots, setSnapshots] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
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
          {!snapshots.length ? (
            <div className="panel p-4 text-sm text-gray-500">
              No saved snapshot for {selectedDate ? formatDate(selectedDate) : 'this date'} yet.
            </div>
          ) : snapshots.map((snapshot) => (
            <SnapshotCard key={`${snapshot.snapshot_date}-${snapshot.algo_id}`} snapshot={snapshot} />
          ))}
        </div>
      </div>
    </section>
  );
}

function SnapshotCard({ snapshot }: { snapshot: any }) {
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
        <div className={`num text-lg font-semibold ${pnlColor(Number(summary.realized_net_pnl || 0))}`}>
          {formatMoney(summary.realized_net_pnl)}
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <MiniMetric label="Cash" value={formatMoney(summary.cash)} />
        <MiniMetric label="Trades" value={`${summary.trade_count_today || 0}`} />
        <MiniMetric label="Buy / Sell" value={`${summary.buy_count_today || 0}B ${summary.sell_count_today || 0}S`} />
        <MiniMetric label="Gross" value={formatMoney(summary.realized_gross_pnl)} />
        <MiniMetric label="Charges" value={formatMoney(summary.realized_charges)} />
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
          {rows.slice(0, 50).map((row, index) => (
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
              {row.entry_trigger && <div className="mt-1 text-[11px] text-gray-500">{row.entry_trigger}</div>}
            </div>
          ))}
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

function formatMoney(value: unknown) {
  const number = Number(value || 0);
  return `Rs ${number.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function formatNumber(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return number.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function pnlColor(value: number) {
  if (value > 0) return 'text-[#22c55e]';
  if (value < 0) return 'text-[#ef4444]';
  return 'text-gray-100';
}
