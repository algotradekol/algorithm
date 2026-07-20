'use client';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api';
import { Table } from './AlgoTab';

const RESOLUTIONS = ['5', '15', '60', 'D'];

export default function HistoryTab() {
  const [algoId, setAlgoId] = useState('algo1');
  const [days, setDays] = useState(30);
  const [resolution, setResolution] = useState('15');
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [symbol, setSymbol] = useState('');
  const [dailyHistory, setDailyHistory] = useState<any[]>([]);
  const [marketHistory, setMarketHistory] = useState<any[]>([]);
  const [recentTrades, setRecentTrades] = useState<any[]>([]);
  const [error, setError] = useState('');
  const [marketError, setMarketError] = useState('');
  const [marketLoading, setMarketLoading] = useState(false);
  const [tokenStatus, setTokenStatus] = useState<any>(null);
  const [tokenStatusError, setTokenStatusError] = useState('');

  useEffect(() => {
    api.watchlist().then((result) => {
      const symbols = result.symbols || [];
      setWatchlist(symbols);
      setSymbol((current) => current || symbols[0] || '');
    }).catch((e: any) => {
      setError(e?.message || 'Failed to load watchlist');
      console.error(e);
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setMarketLoading(Boolean(symbol));
      const [historyResult, tradesResult, marketResult] = await Promise.allSettled([
          api.history(algoId, days),
          api.trades(algoId),
          symbol ? api.marketHistory(symbol, Math.min(days, 30), resolution) : Promise.resolve({ candles: [] }),
      ]);
      if (cancelled) return;

      if (historyResult.status === 'fulfilled') {
        setDailyHistory(historyResult.value);
      }
      if (tradesResult.status === 'fulfilled') {
        setRecentTrades(tradesResult.value.slice(0, 25));
      }
      if (marketResult.status === 'fulfilled') {
        setMarketHistory(marketResult.value.candles || []);
        setMarketError(marketResult.value.warning || '');
      } else {
        setMarketHistory([]);
        setMarketError(marketResult.reason?.message || 'Historical price data is temporarily unavailable');
      }
      setMarketLoading(false);

      const primaryFailures = [historyResult, tradesResult]
        .filter((result) => result.status === 'rejected')
        .map((result) => (result as PromiseRejectedResult).reason?.message || 'Failed to load history');
      setError(primaryFailures[0] || '');
    }
    load();
    return () => { cancelled = true; };
  }, [algoId, days, resolution, symbol]);

  useEffect(() => {
    let cancelled = false;
    async function loadTokenStatus() {
      try {
        const result = await api.fyersTokenStatus();
        if (!cancelled) {
          setTokenStatus(result);
          setTokenStatusError('');
        }
      } catch (e: any) {
        if (!cancelled) setTokenStatusError(e?.message || 'Failed to load token refresh status');
      }
    }
    loadTokenStatus();
    const interval = window.setInterval(loadTokenStatus, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  return (
    <section
      className="space-y-4"
      data-ai-section="History"
      data-ai-history-algo={algoId}
      data-ai-history-days={days}
      data-ai-history-symbol={symbol}
      data-ai-history-resolution={resolution}
      data-ai-history-candle-count={marketHistory.length}
    >
      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}

      <TokenRefreshPanel status={tokenStatus} error={tokenStatusError} />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label>
          <div className="label mb-1">Algo</div>
          <select value={algoId} onChange={(e) => setAlgoId(e.target.value)} className="control">
            <option value="algo1">Algo 1</option>
            <option value="algo2">Algo 2</option>
            <option value="algo3">Algo 3</option>
            <option value="algo4">Algo 4</option>
            <option value="algo5">Algo 5</option>
          </select>
        </label>
        <label>
          <div className="label mb-1">Days</div>
          <input type="number" min={1} max={180} value={days} onChange={(e) => setDays(Number(e.target.value) || 30)} className="control num" />
        </label>
        <label>
          <div className="label mb-1">Symbol</div>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)} className="control">
            {watchlist.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <div className="label mb-1">Resolution</div>
          <select value={resolution} onChange={(e) => setResolution(e.target.value)} className="control">
            {RESOLUTIONS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      </div>

      <section>
        <div className="mb-2 flex items-center justify-between gap-4">
          <h3 className="label">Historical Price Candles</h3>
          <div className="text-xs text-gray-500">Mouse wheel over chart to zoom in/out</div>
        </div>
        {marketError && <p className="mb-3 text-sm text-[#f59e0b]">{marketError}</p>}
        <ZoomableCandleChart candles={marketHistory} symbol={symbol} resolution={resolution} loading={marketLoading} warning={marketError} />
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <section>
          <h3 className="label mb-2">Daily Performance</h3>
          <Table rows={dailyHistory} columns={['date', 'trade_count', 'gross_pnl', 'charges', 'net_pnl']} />
        </section>

        <section>
          <h3 className="label mb-2">Recent Trade Logs</h3>
          <Table rows={recentTrades} columns={['exit_time', 'symbol', 'side', 'qty', 'entry_price', 'exit_price', 'exit_reason', 'net_pnl']} />
        </section>
      </div>

      <section>
        <h3 className="label mb-2">Visible Candle Details</h3>
        <Table rows={marketHistory.slice(-30).reverse()} columns={['time', 'open', 'high', 'low', 'close', 'volume']} />
      </section>
    </section>
  );
}

function TokenRefreshPanel({ status, error }: { status: any; error: string }) {
  const daysLeft = Number(status?.refresh_token_days_left);
  const hasRefreshToken = Boolean(status?.refresh_token_present);
  const lastError = status?.last_refresh_error;
  return (
    <section className="rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="label">Fyers Token Refresh Tracker</h3>
          <p className="mt-1 text-xs text-gray-500">Auto-refresh runs daily after 08:30 IST while the refresh token is valid.</p>
        </div>
        <div className={`inline-flex items-center gap-2 rounded border px-2 py-1 text-xs font-semibold ${
          hasRefreshToken ? 'border-[#22c55e]/40 text-[#22c55e]' : 'border-[#f59e0b]/40 text-[#f59e0b]'
        }`}>
          <i className={hasRefreshToken ? 'ri-shield-check-fill text-sm' : 'ri-error-warning-fill text-sm'} />
          {hasRefreshToken ? 'Refresh token saved' : 'Manual login needed'}
        </div>
      </div>

      {error && <p className="mt-3 text-xs text-[#ef4444]">{error}</p>}

      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <TokenStat label="Days left" value={Number.isFinite(daysLeft) ? `${daysLeft} days` : '--'} tone={daysLeft <= 2 ? 'text-[#f59e0b]' : 'text-gray-100'} />
        <TokenStat label="Refresh token expires" value={formatDateTime(status?.refresh_token_estimated_expires_at)} />
        <TokenStat label="Last access token" value={formatDateTime(status?.access_token_updated_at)} />
        <TokenStat label="Last attempt" value={formatDateTime(status?.last_refresh_attempt_at)} />
      </div>

      {lastError && (
        <div className="mt-3 rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-xs text-[#ef4444]">
          <i className="ri-error-warning-fill mr-1" />
          {lastError}
        </div>
      )}

      <div className="mt-3">
        <div className="label mb-2">Recent Refresh Attempts</div>
        <div className="overflow-x-auto rounded border border-[#1f2937]">
          <table className="w-full min-w-max border-collapse text-xs">
            <thead className="bg-[#0d1117]">
              <tr>
                <th className="table-cell label">Time</th>
                <th className="table-cell label">Status</th>
                <th className="table-cell label">Details</th>
              </tr>
            </thead>
            <tbody>
              {!status?.logs?.length ? (
                <tr><td colSpan={3} className="table-cell text-gray-500">No refresh attempts logged yet.</td></tr>
              ) : status.logs.slice(0, 8).map((log: any, index: number) => (
                <tr key={log.id || index} className={index % 2 === 0 ? 'bg-[#111827]' : 'bg-[#0d1117]'}>
                  <td className="table-cell num text-gray-100">{formatDateTime(log.attempted_at)}</td>
                  <td className={`table-cell font-semibold ${log.status === 'success' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>
                    <i className={`${log.status === 'success' ? 'ri-checkbox-circle-fill' : 'ri-close-circle-fill'} mr-1 text-sm`} />
                    {log.status}
                  </td>
                  <td className="table-cell max-w-xl truncate text-gray-500">{log.error || 'Access token refreshed successfully'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function TokenStat({ label, value, tone = 'text-gray-100' }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#0d1117] p-2">
      <div className="label text-[10px]">{label}</div>
      <div className={`num mt-1 text-xs ${tone}`}>{value}</div>
    </div>
  );
}

function ZoomableCandleChart({
  candles,
  symbol,
  resolution,
  loading,
  warning,
}: {
  candles: any[];
  symbol: string;
  resolution: string;
  loading: boolean;
  warning: string;
}) {
  const [visibleCount, setVisibleCount] = useState(80);
  const [offsetFromEnd, setOffsetFromEnd] = useState(0);
  const [crosshair, setCrosshair] = useState<{ x: number; y: number } | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);
  const pinchRef = useRef<{ distance: number; ratio: number } | null>(null);

  useEffect(() => {
    setVisibleCount(80);
    setOffsetFromEnd(0);
  }, [symbol, resolution, candles.length]);

  const normalized = useMemo(() => candles.map((candle) => ({
    ...candle,
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
    volume: Number(candle.volume || 0),
  })).filter((candle) => Number.isFinite(candle.close)), [candles]);

  const maxVisible = Math.max(10, normalized.length);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const chartElement = chart;

    function handleWheel(event: WheelEvent) {
      event.preventDefault();
      const zoomingIn = event.deltaY < 0;
      const rect = chartElement.getBoundingClientRect();
      const pointerRatio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));

      setVisibleCount((current) => {
        const currentVisible = Math.min(Math.max(current, 10), maxVisible);
        const currentMaxOffset = Math.max(0, normalized.length - currentVisible);
        const currentOffset = Math.min(offsetFromEnd, currentMaxOffset);
        const currentEnd = normalized.length - currentOffset;
        const currentStart = Math.max(0, currentEnd - currentVisible);
        const anchorIndex = currentStart + pointerRatio * Math.max(0, currentVisible - 1);
        const step = Math.max(4, Math.round(currentVisible * 0.12));
        const nextVisible = Math.min(maxVisible, Math.max(10, zoomingIn ? currentVisible - step : currentVisible + step));
        const nextStart = Math.round(anchorIndex - pointerRatio * Math.max(0, nextVisible - 1));
        const clampedStart = Math.min(Math.max(0, nextStart), Math.max(0, normalized.length - nextVisible));
        setOffsetFromEnd(Math.max(0, normalized.length - (clampedStart + nextVisible)));
        return nextVisible;
      });
    }

    chartElement.addEventListener('wheel', handleWheel, { passive: false, capture: true });
    return () => chartElement.removeEventListener('wheel', handleWheel, { capture: true });
  }, [maxVisible, normalized.length, offsetFromEnd]);

  if (loading) return <p className="rounded border border-[#1f2937] bg-[#111827] p-4 text-sm text-gray-500">Loading candle history...</p>;
  if (!normalized.length) {
    return (
      <div className="rounded border border-[#1f2937] bg-[#111827] p-4 text-sm text-gray-500">
        <p>No candle history available for {symbol || 'this symbol'}.</p>
        {warning && <p className="mt-2 text-[#f59e0b]">{warning}</p>}
      </div>
    );
  }

  const clampedVisible = Math.min(Math.max(visibleCount, 10), maxVisible);
  const maxOffset = Math.max(0, normalized.length - clampedVisible);
  const clampedOffset = Math.min(offsetFromEnd, maxOffset);
  const end = normalized.length - clampedOffset;
  const start = Math.max(0, end - clampedVisible);
  const visible = normalized.slice(start, end);

  const high = Math.max(...visible.map((candle) => candle.high));
  const low = Math.min(...visible.map((candle) => candle.low));
  const maxVolume = Math.max(...visible.map((candle) => candle.volume), 1);
  const priceSpan = high - low || 1;
  const width = 1200;
  const priceHeight = 330;
  const volumeHeight = 70;
  const totalHeight = priceHeight + volumeHeight + 34;
  const candleWidth = width / Math.max(visible.length, 1);
  const activeIndex = crosshair ? Math.min(visible.length - 1, Math.max(0, Math.floor(crosshair.x / candleWidth))) : null;
  const activeCandle = activeIndex !== null ? visible[activeIndex] : null;
  const activeX = activeIndex !== null ? activeIndex * candleWidth + candleWidth / 2 : 0;
  const activePrice = crosshair ? high - ((crosshair.y - 16) / (priceHeight - 32)) * priceSpan : null;

  function y(price: number) {
    return 16 + ((high - price) / priceSpan) * (priceHeight - 32);
  }

  function handleMouseMove(event: React.MouseEvent<SVGSVGElement>) {
    const svg = event.currentTarget;
    const rect = svg.getBoundingClientRect();
    const x = Math.min(width, Math.max(0, (event.clientX - rect.left) / rect.width * width));
    const yPos = Math.min(priceHeight + 18, Math.max(0, (event.clientY - rect.top) / rect.height * totalHeight));
    setCrosshair({ x, y: yPos });
  }

  const first = visible[0];
  const last = visible[visible.length - 1];
  const change = last.close - first.open;
  const changePct = first.open ? change / first.open * 100 : 0;

  function zoomAtRatio(ratio: number, zoomingIn: boolean) {
    const currentVisible = clampedVisible;
    const anchorIndex = start + ratio * Math.max(0, currentVisible - 1);
    const step = Math.max(4, Math.round(currentVisible * (zoomingIn ? 0.25 : 0.35)));
    const nextVisible = Math.min(maxVisible, Math.max(10, zoomingIn ? currentVisible - step : currentVisible + step));
    const nextStart = Math.round(anchorIndex - ratio * Math.max(0, nextVisible - 1));
    const clampedStart = Math.min(Math.max(0, nextStart), Math.max(0, normalized.length - nextVisible));
    setVisibleCount(nextVisible);
    setOffsetFromEnd(Math.max(0, normalized.length - (clampedStart + nextVisible)));
  }

  function handleTouchStart(event: React.TouchEvent<HTMLDivElement>) {
    if (event.touches.length !== 2 || !chartRef.current) return;
    event.preventDefault();
    const rect = chartRef.current.getBoundingClientRect();
    const midpointX = (event.touches[0].clientX + event.touches[1].clientX) / 2;
    pinchRef.current = {
      distance: touchDistance(event.touches[0], event.touches[1]),
      ratio: Math.min(1, Math.max(0, (midpointX - rect.left) / rect.width)),
    };
  }

  function handleTouchMove(event: React.TouchEvent<HTMLDivElement>) {
    if (event.touches.length !== 2 || !pinchRef.current) return;
    event.preventDefault();
    const nextDistance = touchDistance(event.touches[0], event.touches[1]);
    const previousDistance = pinchRef.current.distance;
    if (!previousDistance) return;
    const scale = nextDistance / previousDistance;
    if (Math.abs(scale - 1) < 0.06) return;
    zoomAtRatio(pinchRef.current.ratio, scale > 1);
    pinchRef.current = { ...pinchRef.current, distance: nextDistance };
  }

  function handleTouchEnd(event: React.TouchEvent<HTMLDivElement>) {
    if (event.touches.length < 2) pinchRef.current = null;
  }

  return (
    <div
      className="rounded border border-[#1f2937] bg-[#111827] p-3"
      data-ai-chart="candlestick"
      data-ai-chart-symbol={symbol}
      data-ai-chart-resolution={resolution}
      data-ai-chart-total-candles={normalized.length}
      data-ai-chart-visible-range={`${start + 1}-${end}`}
      data-ai-chart-open={formatNumber(first.open)}
      data-ai-chart-high={formatNumber(high)}
      data-ai-chart-low={formatNumber(low)}
      data-ai-chart-close={formatNumber(last.close)}
      data-ai-chart-change={`${change >= 0 ? '+' : ''}${formatNumber(change)} (${changePct.toFixed(2)}%)`}
      data-ai-chart-first-time={first.time}
      data-ai-chart-last-time={last.time}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="font-mono text-sm font-semibold text-gray-100">{symbol || 'Symbol'} / {resolution}</div>
          <div className="mt-1 text-xs text-gray-500">
            Showing candles {start + 1}-{end} of {normalized.length}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-4 text-xs">
          <Stat label="Open" value={formatNumber(first.open)} />
          <Stat label="High" value={formatNumber(high)} />
          <Stat label="Low" value={formatNumber(low)} />
          <Stat label="Close" value={formatNumber(last.close)} />
          <Stat label="Change" value={`${change >= 0 ? '+' : ''}${formatNumber(change)} (${changePct.toFixed(2)}%)`} tone={change >= 0 ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => zoomAtRatio(0.5, true)} className="rounded border border-[#3b82f6] px-2 py-1 text-xs text-[#3b82f6]">Zoom In</button>
          <button onClick={() => zoomAtRatio(0.5, false)} className="rounded border border-[#3b82f6] px-2 py-1 text-xs text-[#3b82f6]">Zoom Out</button>
          <button onClick={() => { setVisibleCount(80); setOffsetFromEnd(0); }} className="rounded border border-[#1f2937] px-2 py-1 text-xs text-gray-500">Reset</button>
        </div>
      </div>

      <div
        ref={chartRef}
        onWheelCapture={(event) => event.preventDefault()}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
        onTouchCancel={() => { pinchRef.current = null; }}
        className="overscroll-contain overflow-x-auto border border-[#1f2937] bg-[#0a0e14]"
        style={{ overscrollBehavior: 'contain', touchAction: 'none' }}
      >
        <svg
          viewBox={`0 0 ${width} ${totalHeight}`}
          className="h-[440px] min-w-[900px] w-full cursor-crosshair"
          onMouseMove={handleMouseMove}
          onMouseLeave={() => setCrosshair(null)}
        >
          {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
            const price = high - priceSpan * ratio;
            const lineY = y(price);
            return (
              <g key={ratio}>
                <line x1={0} x2={width} y1={lineY} y2={lineY} stroke="#1f2937" strokeWidth="1" />
                <text x={8} y={lineY - 4} fill="#6b7280" fontSize="11" fontFamily="ui-monospace">{formatNumber(price)}</text>
              </g>
            );
          })}

          {visible.map((candle, index) => {
            const x = index * candleWidth + candleWidth / 2;
            const openY = y(candle.open);
            const closeY = y(candle.close);
            const highY = y(candle.high);
            const lowY = y(candle.low);
            const bullish = candle.close >= candle.open;
            const color = bullish ? '#22c55e' : '#ef4444';
            const bodyTop = Math.min(openY, closeY);
            const bodyHeight = Math.max(1, Math.abs(closeY - openY));
            const bodyWidth = Math.max(2, candleWidth * 0.58);
            const volumeBarHeight = candle.volume / maxVolume * (volumeHeight - 10);
            const volumeY = priceHeight + 18 + (volumeHeight - volumeBarHeight);
            return (
              <g key={`${candle.time}-${index}`}>
                <title>{`${candle.time}\nO ${formatNumber(candle.open)} H ${formatNumber(candle.high)} L ${formatNumber(candle.low)} C ${formatNumber(candle.close)}\nVol ${candle.volume.toLocaleString('en-IN')}`}</title>
                <line x1={x} x2={x} y1={highY} y2={lowY} stroke={color} strokeWidth="1.2" />
                <rect x={x - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={bodyHeight} fill={color} opacity={bullish ? 0.85 : 0.75} />
                <rect x={x - bodyWidth / 2} y={volumeY} width={bodyWidth} height={volumeBarHeight} fill={color} opacity="0.35" />
              </g>
            );
          })}

          {crosshair && activeCandle && activePrice !== null && (
            <g pointerEvents="none">
              <line x1={activeX} x2={activeX} y1={0} y2={priceHeight + 18} stroke="#9ca3af" strokeDasharray="5 5" strokeWidth="1" opacity="0.75" />
              <line x1={0} x2={width} y1={crosshair.y} y2={crosshair.y} stroke="#9ca3af" strokeDasharray="5 5" strokeWidth="1" opacity="0.75" />
              <line x1={0} x2={width} y1={y(activeCandle.close)} y2={y(activeCandle.close)} stroke="#22c55e" strokeDasharray="2 2" strokeWidth="1" opacity="0.85" />
              <rect x={width - 88} y={Math.max(2, Math.min(priceHeight - 20, crosshair.y - 10))} width={82} height={20} fill="#111827" stroke="#1f2937" />
              <text x={width - 82} y={Math.max(15, Math.min(priceHeight - 7, crosshair.y + 4))} fill="#e5e7eb" fontSize="11" fontFamily="ui-monospace">
                {formatNumber(activePrice)}
              </text>
              <rect x={Math.min(width - 250, activeX + 10)} y={18} width={240} height={94} fill="#111827" stroke="#1f2937" />
              <text x={Math.min(width - 240, activeX + 20)} y={38} fill="#e5e7eb" fontSize="11" fontFamily="ui-monospace">{activeCandle.time}</text>
              <text x={Math.min(width - 240, activeX + 20)} y={56} fill="#9ca3af" fontSize="11" fontFamily="ui-monospace">
                O {formatNumber(activeCandle.open)}  H {formatNumber(activeCandle.high)}
              </text>
              <text x={Math.min(width - 240, activeX + 20)} y={74} fill="#9ca3af" fontSize="11" fontFamily="ui-monospace">
                L {formatNumber(activeCandle.low)}  C {formatNumber(activeCandle.close)}
              </text>
              <text x={Math.min(width - 240, activeX + 20)} y={92} fill="#9ca3af" fontSize="11" fontFamily="ui-monospace">
                Vol {activeCandle.volume.toLocaleString('en-IN')}
              </text>
            </g>
          )}

          <line x1={0} x2={width} y1={priceHeight + 18} y2={priceHeight + 18} stroke="#1f2937" />
          <text x={8} y={totalHeight - 8} fill="#6b7280" fontSize="11" fontFamily="ui-monospace">
            {`${first.time} -> ${last.time}`}
          </text>
        </svg>
      </div>
    </div>
  );
}

function Stat({ label, value, tone = 'text-gray-100' }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`num mt-1 ${tone}`}>{value}</div>
    </div>
  );
}

function formatNumber(value: number) {
  return value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  });
}

function touchDistance(first: React.Touch, second: React.Touch) {
  return Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY);
}
