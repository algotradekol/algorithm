'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

type OverlayState = {
  ema: boolean;
  setups: boolean;
  trades: boolean;
  levels: boolean;
  trailing: boolean;
};

type Props = {
  days: any[];
  selectedDate: string;
  onSelectedDateChange: (value: string) => void;
  selectedTradeId: string | null;
  onSelectedTradeIdChange: (value: string | null) => void;
  overlays: OverlayState;
  onOverlaysChange: (value: OverlayState) => void;
  onOpenModal: () => void;
  expanded?: boolean;
};

export default function SilverBacktestChart({
  days,
  selectedDate,
  onSelectedDateChange,
  selectedTradeId,
  onSelectedTradeIdChange,
  overlays,
  onOverlaysChange,
  onOpenModal,
  expanded = false,
}: Props) {
  const selectedDay = days.find((day) => day.date === selectedDate) || days[0];
  const chart = selectedDay?.chart || {};
  const chartCandles = Array.isArray(chart.candles) ? chart.candles : [];
  const chartTrades = Array.isArray(chart.trades) ? chart.trades : [];
  const chartSetups = Array.isArray(chart.setups) ? chart.setups : [];
  const viewportHint = chart.viewport_hint || {};

  const normalized = useMemo(() => chartCandles.map((candle: any) => ({
    ...candle,
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
    volume: Number(candle.volume || 0),
    ema20: Number(candle.ema20),
    timeMs: parseMaybeDate(candle.time)?.getTime() ?? null,
  })).filter((candle: any) => Number.isFinite(candle.close)), [chartCandles]);

  const selectedTrade = chartTrades.find((trade: any) => trade.trade_id === selectedTradeId) || null;
  const [visibleCount, setVisibleCount] = useState(80);
  const [offsetFromEnd, setOffsetFromEnd] = useState(0);
  const [crosshair, setCrosshair] = useState<{ x: number; y: number } | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);
  const pinchRef = useRef<{ distance: number; ratio: number } | null>(null);
  const dragRef = useRef<{ x: number; offset: number } | null>(null);

  const maxVisible = Math.max(10, normalized.length);
  const clampedVisible = Math.min(Math.max(visibleCount, 10), maxVisible);
  const maxOffset = Math.max(0, normalized.length - clampedVisible);
  const clampedOffset = Math.min(offsetFromEnd, maxOffset);
  const end = normalized.length - clampedOffset;
  const start = Math.max(0, end - clampedVisible);
  const visible = normalized.slice(start, end);

  useEffect(() => {
    fitInitialViewport();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate, selectedTradeId, normalized.length]);

  useEffect(() => {
    const chartElement = chartRef.current;
    if (!chartElement) return;

    function handleWheel(event: WheelEvent) {
      event.preventDefault();
      if (!normalized.length) return;
      const rect = chartRef.current?.getBoundingClientRect();
      if (!rect) return;
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(rect.width, 1)));
      zoomAtRatio(ratio, event.deltaY < 0);
    }

    chartElement.addEventListener('wheel', handleWheel, { passive: false, capture: true });
    return () => chartElement.removeEventListener('wheel', handleWheel, { capture: true });
  }, [normalized.length, start, clampedVisible]);

  if (!selectedDay) return null;

  if (!normalized.length) {
    return (
      <section className="panel p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Silver Replay Chart</h3>
            <p className="mt-1 text-xs text-gray-500">No chartable Silver candles were returned for {selectedDay.date}.</p>
          </div>
          <DateSelector days={days} selectedDate={selectedDate} onSelectedDateChange={onSelectedDateChange} />
        </div>
      </section>
    );
  }

  const width = expanded ? 1720 : 1460;
  const priceHeight = expanded ? 420 : 350;
  const volumeHeight = 76;
  const totalHeight = priceHeight + volumeHeight + 34;
  const candleWidth = width / Math.max(visible.length, 1);
  const high = Math.max(...visible.map((candle: any) => candle.high));
  const low = Math.min(...visible.map((candle: any) => candle.low));
  const maxVolume = Math.max(...visible.map((candle: any) => candle.volume), 1);
  const priceSpan = high - low || 1;
  const first = visible[0];
  const last = visible[visible.length - 1];
  const change = last.close - first.open;
  const changePct = first.open ? change / first.open * 100 : 0;
  const activeIndex = crosshair ? Math.min(visible.length - 1, Math.max(0, Math.floor(crosshair.x / candleWidth))) : null;
  const activeCandle = activeIndex !== null ? visible[activeIndex] : null;
  const statCandle = activeCandle || last;
  const activeX = activeIndex !== null ? activeIndex * candleWidth + candleWidth / 2 : 0;
  const activePrice = crosshair ? high - ((crosshair.y - 16) / (priceHeight - 32)) * priceSpan : null;
  const tooltipWidth = 290;
  const tooltipHeight = 128;
  const tooltipGap = 18;
  const tooltipX = activeX > width / 2
    ? Math.max(8, activeX - tooltipWidth - tooltipGap)
    : Math.min(width - tooltipWidth - 8, activeX + tooltipGap);
  const tooltipY = crosshair && crosshair.y < tooltipHeight + 34
    ? Math.min(priceHeight - tooltipHeight - 8, crosshair.y + tooltipGap)
    : 18;
  const visibleSetupOverlays = chartSetups
    .map((setup: any) => ({ ...setup, index: indexForTime(normalized, setup.time) }))
    .filter((setup: any) => setup.index >= start && setup.index < end);
  const visibleTradeOverlays = chartTrades
    .map((trade: any) => ({
      ...trade,
      entryIndex: indexForTime(normalized, trade.entry_time),
      exitIndex: indexForTime(normalized, trade.exit_time),
      selected: trade.trade_id === selectedTradeId,
    }))
    .filter((trade: any) => trade.entryIndex < end && trade.exitIndex >= start);
  const selectedTradeOverlay = visibleTradeOverlays.find((trade: any) => trade.selected) || (
    selectedTrade
      ? {
          ...selectedTrade,
          entryIndex: indexForTime(normalized, selectedTrade.entry_time),
          exitIndex: indexForTime(normalized, selectedTrade.exit_time),
          selected: true,
        }
      : null
  );
  const timeTicks = buildTimeTicks(visible, start, candleWidth, expanded ? 8 : 6);

  function y(price: number) {
    return 16 + ((high - price) / priceSpan) * (priceHeight - 32);
  }

  function fitInitialViewport() {
    if (!normalized.length) {
      setVisibleCount(80);
      setOffsetFromEnd(0);
      return;
    }
    if (selectedTrade) {
      fitTradeWindow(selectedTrade, 8);
      return;
    }
    const startTime = viewportHint.start_time || normalized[0]?.time;
    const endTime = viewportHint.end_time || normalized[normalized.length - 1]?.time;
    fitTimeRange(startTime, endTime, viewportHint.mode === 'trade_window' ? 2 : 0);
  }

  function fitTimeRange(startTime: string | null | undefined, endTime: string | null | undefined, padBars = 0) {
    if (!normalized.length) return;
    const startIndex = Math.max(0, indexForTime(normalized, startTime));
    const endIndex = Math.max(startIndex, indexForTime(normalized, endTime));
    const paddedStart = Math.max(0, startIndex - padBars);
    const paddedEnd = Math.min(normalized.length - 1, endIndex + padBars);
    const nextVisible = Math.min(maxVisible, Math.max(10, paddedEnd - paddedStart + 1));
    setVisibleCount(nextVisible);
    setOffsetFromEnd(Math.max(0, normalized.length - (paddedStart + nextVisible)));
  }

  function fitTradeWindow(trade: any, padBars = 8) {
    fitTimeRange(trade?.entry_time, trade?.exit_time || trade?.entry_time, padBars);
  }

  function showFullDay() {
    setVisibleCount(maxVisible);
    setOffsetFromEnd(0);
  }

  function zoomAtRatio(ratio: number, zoomingIn: boolean) {
    const currentVisible = clampedVisible;
    const anchorIndex = start + ratio * Math.max(0, currentVisible - 1);
    const step = Math.max(4, Math.round(currentVisible * (zoomingIn ? 0.22 : 0.28)));
    const nextVisible = Math.min(maxVisible, Math.max(10, zoomingIn ? currentVisible - step : currentVisible + step));
    const nextStart = Math.round(anchorIndex - ratio * Math.max(0, nextVisible - 1));
    const clampedStart = Math.min(Math.max(0, nextStart), Math.max(0, normalized.length - nextVisible));
    setVisibleCount(nextVisible);
    setOffsetFromEnd(Math.max(0, normalized.length - (clampedStart + nextVisible)));
  }

  function handleMouseMove(event: React.MouseEvent<SVGSVGElement>) {
    const svg = event.currentTarget;
    const rect = svg.getBoundingClientRect();
    const x = Math.min(width, Math.max(0, ((event.clientX - rect.left) / Math.max(rect.width, 1)) * width));
    const yPos = Math.min(priceHeight + 18, Math.max(0, ((event.clientY - rect.top) / Math.max(rect.height, 1)) * totalHeight));
    setCrosshair({ x, y: yPos });
  }

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = { x: event.clientX, offset: clampedOffset };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(event: React.PointerEvent<HTMLDivElement>) {
    if (!dragRef.current || !normalized.length) return;
    const deltaX = event.clientX - dragRef.current.x;
    const movedBars = Math.round(deltaX / Math.max(candleWidth, 1));
    const nextOffset = Math.min(maxOffset, Math.max(0, dragRef.current.offset - movedBars));
    setOffsetFromEnd(nextOffset);
  }

  function handlePointerUp(event: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function handleTouchStart(event: React.TouchEvent<HTMLDivElement>) {
    if (event.touches.length !== 2 || !chartRef.current) return;
    event.preventDefault();
    const rect = chartRef.current.getBoundingClientRect();
    const midpointX = (event.touches[0].clientX + event.touches[1].clientX) / 2;
    pinchRef.current = {
      distance: touchDistance(event.touches[0], event.touches[1]),
      ratio: Math.min(1, Math.max(0, (midpointX - rect.left) / Math.max(rect.width, 1))),
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

  return (
    <section className="panel p-4">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-100">Silver Replay Chart</h3>
          <p className="mt-1 text-xs text-gray-500">Zoom with mouse wheel, drag to pan, and inspect the replayed 15-minute candles with EMA20, setups, entries, exits, and trailing moves.</p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <DateSelector days={days} selectedDate={selectedDate} onSelectedDateChange={onSelectedDateChange} />
          {!expanded && (
            <button onClick={onOpenModal} className="rounded border border-[#3b82f6]/50 bg-[#3b82f6]/10 px-3 py-2 text-xs font-semibold text-[#93c5fd]">
              View chart
            </button>
          )}
        </div>
      </div>

      <div className="mt-4 space-y-3">
        <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-mono text-sm font-semibold text-gray-100">{chart.symbol} / 15m / {selectedDay.date}</div>
              <div className="mt-1 text-xs text-gray-500">
                {activeCandle ? `Focused candle: ${activeCandle.time}` : `Showing candles ${start + 1}-${end} of ${normalized.length}`}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-4 text-xs">
              <Stat label="Open" value={formatNumber(statCandle.open)} />
              <Stat label="High" value={formatNumber(statCandle.high)} />
              <Stat label="Low" value={formatNumber(statCandle.low)} />
              <Stat label="Close" value={formatNumber(statCandle.close)} />
              <Stat label="Change" value={`${change >= 0 ? '+' : ''}${formatNumber(change)} (${changePct.toFixed(2)}%)`} tone={change >= 0 ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
            </div>
          </div>

          <div className="mb-3 flex flex-wrap items-center gap-2">
            <button onClick={() => zoomAtRatio(0.5, true)} className="rounded border border-[#3b82f6] px-2 py-1 text-xs text-[#3b82f6]">Zoom In</button>
            <button onClick={() => zoomAtRatio(0.5, false)} className="rounded border border-[#3b82f6] px-2 py-1 text-xs text-[#3b82f6]">Zoom Out</button>
            <button onClick={showFullDay} className="rounded border border-[#1f2937] px-2 py-1 text-xs text-gray-400">Show full day</button>
            <button onClick={() => selectedTrade && fitTradeWindow(selectedTrade, 8)} disabled={!selectedTrade} className="rounded border border-[#1f2937] px-2 py-1 text-xs text-gray-400 disabled:cursor-not-allowed disabled:opacity-50">Fit trade window</button>
            <button onClick={() => selectedTrade && fitTradeWindow(selectedTrade, 3)} disabled={!selectedTrade} className="rounded border border-[#1f2937] px-2 py-1 text-xs text-gray-400 disabled:cursor-not-allowed disabled:opacity-50">Focus selected trade</button>
          </div>

          <div
            ref={chartRef}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerCancel={() => { dragRef.current = null; }}
            onTouchStart={handleTouchStart}
            onTouchMove={handleTouchMove}
            onTouchEnd={(event) => { if (event.touches.length < 2) pinchRef.current = null; }}
            onTouchCancel={() => { pinchRef.current = null; }}
            className="overscroll-contain overflow-hidden border border-[#1f2937] bg-[#0a0e14]"
            style={{ overscrollBehavior: 'contain', touchAction: 'none' }}
          >
            <svg
              viewBox={`0 0 ${width} ${totalHeight}`}
              className={expanded ? 'h-[560px] w-full cursor-crosshair' : 'h-[450px] w-full cursor-crosshair'}
              onMouseMove={handleMouseMove}
              onMouseLeave={() => setCrosshair(null)}
            >
              {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
                const price = high - priceSpan * ratio;
                const lineY = y(price);
                return (
                  <g key={ratio}>
                    <line x1={0} x2={width} y1={lineY} y2={lineY} stroke="#1f2937" strokeWidth="1" />
                    <rect x={width - 116} y={lineY - 14} width={108} height={22} rx={4} fill="#111827" stroke="#334155" />
                    <text x={width - 16} y={lineY + 1} textAnchor="end" fill="#cbd5e1" fontSize="12.5" fontFamily="ui-monospace">{formatNumber(price)}</text>
                  </g>
                );
              })}

              {overlays.ema && (
                <path
                  d={visible.map((candle: any, index: number) => {
                    const pointX = index * candleWidth + candleWidth / 2;
                    const pointY = y(candle.ema20);
                    return `${index === 0 ? 'M' : 'L'} ${pointX} ${pointY}`;
                  }).join(' ')}
                  fill="none"
                  stroke="#3b82f6"
                  strokeWidth="2"
                />
              )}

              {visible.map((candle: any, index: number) => {
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
                    <title>{`${candle.time}\nO ${formatNumber(candle.open)} H ${formatNumber(candle.high)} L ${formatNumber(candle.low)} C ${formatNumber(candle.close)}\nEMA20 ${formatNumber(candle.ema20)}\nVol ${candle.volume.toLocaleString('en-IN')}`}</title>
                    <line x1={x} x2={x} y1={highY} y2={lowY} stroke={color} strokeWidth="1.2" />
                    <rect x={x - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={bodyHeight} fill={color} opacity={bullish ? 0.85 : 0.75} />
                    <rect x={x - bodyWidth / 2} y={volumeY} width={bodyWidth} height={volumeBarHeight} fill={color} opacity="0.35" />
                  </g>
                );
              })}

              {overlays.setups && visibleSetupOverlays.map((setup: any, index: number) => {
                const localIndex = setup.index - start;
                const pointX = localIndex * candleWidth + candleWidth / 2;
                const setupColor = setup.side === 'BUY' ? '#22c55e' : '#ef4444';
                return (
                  <g key={`${setup.side}-${setup.time}-${index}`}>
                    <line x1={pointX} x2={width} y1={y(Number(setup.setup_close))} y2={y(Number(setup.setup_close))} stroke={setupColor} strokeWidth="1.4" opacity="0.45" />
                    <line x1={pointX} x2={width} y1={y(Number(setup.trigger_level))} y2={y(Number(setup.trigger_level))} stroke={setupColor} strokeWidth="1" strokeDasharray="5 4" opacity="0.7" />
                    <circle cx={pointX} cy={y(Number(setup.setup_close))} r={4} fill={setupColor} />
                  </g>
                );
              })}

              {overlays.trades && visibleTradeOverlays.map((trade: any) => {
                const entryIndex = Math.max(start, trade.entryIndex) - start;
                const exitIndex = Math.max(start, trade.exitIndex) - start;
                const entryX = entryIndex * candleWidth + candleWidth / 2;
                const exitX = exitIndex * candleWidth + candleWidth / 2;
                const entryCandle = normalized[Math.max(0, Math.min(normalized.length - 1, trade.entryIndex))];
                const entryY = y(Number(trade.entry_price));
                const exitY = y(Number(trade.exit_price));
                const isBuy = trade.side === 'BUY';
                const color = isBuy ? '#38bdf8' : '#fb7185';
                const exitMarkerFill = '#f8fafc';
                const opacity = trade.selected ? 1 : 0.55;
                const candleBoundaryY = isBuy ? y(Number(entryCandle?.high ?? trade.entry_price)) : y(Number(entryCandle?.low ?? trade.entry_price));
                const arrowGap = trade.selected ? 10 : 8;
                const arrowReach = trade.selected ? 44 : 36;
                const tipY = isBuy ? candleBoundaryY - arrowGap : candleBoundaryY + arrowGap;
                const shaftStartY = isBuy ? tipY - arrowReach : tipY + arrowReach;
                const shaftEndY = isBuy ? tipY - 12 : tipY + 12;
                const arrowHeadPoints = isBuy
                  ? `${entryX - 7},${tipY - 12} ${entryX + 7},${tipY - 12} ${entryX},${tipY}`
                  : `${entryX - 7},${tipY + 12} ${entryX + 7},${tipY + 12} ${entryX},${tipY}`;
                return (
                  <g
                    key={trade.trade_id}
                    opacity={opacity}
                    onClick={() => onSelectedTradeIdChange(trade.trade_id)}
                    style={{ cursor: 'pointer' }}
                  >
                    <line x1={entryX} x2={exitX} y1={entryY} y2={exitY} stroke={color} strokeWidth={trade.selected ? 2.4 : 1.5} strokeDasharray={trade.selected ? undefined : '5 4'} />
                    <line x1={entryX} x2={entryX} y1={shaftStartY} y2={shaftEndY} stroke={color} strokeWidth={trade.selected ? 3 : 2.4} strokeLinecap="round" />
                    <polygon points={arrowHeadPoints} fill={color} stroke="#e5e7eb" strokeWidth="0.8" />
                    <circle cx={exitX} cy={exitY} r={trade.selected ? 5.4 : 4.3} fill={exitMarkerFill} stroke={color} strokeWidth={trade.selected ? 2.4 : 2} opacity="1" />
                  </g>
                );
              })}

              {overlays.levels && selectedTradeOverlay && (
                <g pointerEvents="none">
                  <line x1={Math.max(0, selectedTradeOverlay.entryIndex - start) * candleWidth + candleWidth / 2} x2={width} y1={y(Number(selectedTradeOverlay.initial_sl_price))} y2={y(Number(selectedTradeOverlay.initial_sl_price))} stroke="#f59e0b" strokeWidth="1.5" strokeDasharray="6 4" />
                  <line x1={Math.max(0, selectedTradeOverlay.entryIndex - start) * candleWidth + candleWidth / 2} x2={width} y1={y(Number(selectedTradeOverlay.final_sl_price))} y2={y(Number(selectedTradeOverlay.final_sl_price))} stroke="#f97316" strokeWidth="1.8" />
                  <line x1={Math.max(0, selectedTradeOverlay.entryIndex - start) * candleWidth + candleWidth / 2} x2={width} y1={y(Number(selectedTradeOverlay.target_price))} y2={y(Number(selectedTradeOverlay.target_price))} stroke="#a78bfa" strokeWidth="1.5" strokeDasharray="4 3" />
                </g>
              )}

              {overlays.trailing && selectedTradeOverlay && Array.isArray(selectedTradeOverlay.trailing_moves) && selectedTradeOverlay.trailing_moves.length > 0 && (
                <path
                  d={buildTrailingPath(
                    normalized,
                    start,
                    candleWidth,
                    y,
                    Number(selectedTradeOverlay.initial_sl_price),
                    selectedTradeOverlay.entry_time,
                    selectedTradeOverlay.exit_time,
                    selectedTradeOverlay.trailing_moves,
                    Number(selectedTradeOverlay.final_sl_price),
                  )}
                  fill="none"
                  stroke="#fbbf24"
                  strokeWidth="2"
                />
              )}

              {crosshair && activeCandle && activePrice !== null && (
                <g pointerEvents="none">
                  <line x1={activeX} x2={activeX} y1={0} y2={priceHeight + 18} stroke="#9ca3af" strokeDasharray="5 5" strokeWidth="1" opacity="0.75" />
                  <line x1={0} x2={width} y1={crosshair.y} y2={crosshair.y} stroke="#9ca3af" strokeDasharray="5 5" strokeWidth="1" opacity="0.75" />
                  <rect x={width - 110} y={Math.max(2, Math.min(priceHeight - 24, crosshair.y - 12))} width={102} height={24} fill="#111827" stroke="#1f2937" />
                  <text x={width - 102} y={Math.max(17, Math.min(priceHeight - 6, crosshair.y + 5))} fill="#e5e7eb" fontSize="12.5" fontFamily="ui-monospace">
                    {formatNumber(activePrice)}
                  </text>
                  <rect x={tooltipX} y={tooltipY} width={tooltipWidth} height={tooltipHeight} fill="#111827" stroke="#1f2937" />
                  <text x={tooltipX + 12} y={tooltipY + 22} fill="#e5e7eb" fontSize="12.5" fontFamily="ui-monospace">{activeCandle.time}</text>
                  <text x={tooltipX + 12} y={tooltipY + 44} fill="#9ca3af" fontSize="12.5" fontFamily="ui-monospace">
                    O {formatNumber(activeCandle.open)}  H {formatNumber(activeCandle.high)}
                  </text>
                  <text x={tooltipX + 12} y={tooltipY + 66} fill="#9ca3af" fontSize="12.5" fontFamily="ui-monospace">
                    L {formatNumber(activeCandle.low)}  C {formatNumber(activeCandle.close)}
                  </text>
                  <text x={tooltipX + 12} y={tooltipY + 88} fill="#60a5fa" fontSize="12.5" fontFamily="ui-monospace">
                    EMA20 {formatNumber(activeCandle.ema20)}
                  </text>
                  <text x={tooltipX + 12} y={tooltipY + 110} fill="#9ca3af" fontSize="12.5" fontFamily="ui-monospace">
                    Vol {activeCandle.volume.toLocaleString('en-IN')}
                  </text>
                </g>
              )}

              <line x1={0} x2={width} y1={priceHeight + 18} y2={priceHeight + 18} stroke="#1f2937" />
              {timeTicks.map((tick) => (
                <g key={tick.key}>
                  <line x1={tick.x} x2={tick.x} y1={priceHeight + 18} y2={priceHeight + 24} stroke="#334155" strokeWidth="1" />
                  <text x={tick.x} y={totalHeight - 8} textAnchor="middle" fill="#94a3b8" fontSize="11.5" fontFamily="ui-monospace">
                    {tick.label}
                  </text>
                </g>
              ))}
              <text x={8} y={totalHeight - 22} fill="#64748b" fontSize="11.5" fontFamily="ui-monospace">
                15m candles · IST
              </text>
              <text x={8} y={totalHeight - 8} fill="#6b7280" fontSize="12" fontFamily="ui-monospace">
                {activeCandle ? `Focused ${formatAxisDateTime(activeCandle.time)}  |  window ${formatAxisDateTime(first.time)} -> ${formatAxisDateTime(last.time)}` : `${formatAxisDateTime(first.time)} -> ${formatAxisDateTime(last.time)}`}
              </text>
            </svg>
          </div>
        </div>

        <div className="grid gap-3 xl:grid-cols-[minmax(0,260px)_minmax(0,1fr)_minmax(0,220px)]">
          <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Overlay toggles</div>
            <div className="mt-3 space-y-2 text-sm text-gray-300">
              <Toggle label="EMA20 (blue)" checked={overlays.ema} onChange={() => onOverlaysChange({ ...overlays, ema: !overlays.ema })} />
              <Toggle label="Setup markers" checked={overlays.setups} onChange={() => onOverlaysChange({ ...overlays, setups: !overlays.setups })} />
              <Toggle label="Trades" checked={overlays.trades} onChange={() => onOverlaysChange({ ...overlays, trades: !overlays.trades })} />
              <Toggle label="SL / target" checked={overlays.levels} onChange={() => onOverlaysChange({ ...overlays, levels: !overlays.levels })} />
              <Toggle label="Trailing path" checked={overlays.trailing} onChange={() => onOverlaysChange({ ...overlays, trailing: !overlays.trailing })} />
            </div>
            <p className="mt-3 text-xs text-gray-500">Setup markers stay off by default so the chart opens cleaner and the trade arrows stay easy to read.</p>
          </div>

          <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Selected trade</div>
            {!selectedTrade ? (
              <p className="mt-3 text-sm text-gray-500">No trade selected for this day yet. Pick a trade row or marker to focus it.</p>
            ) : (
              <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2 xl:grid-cols-3">
                <ChartMetric label="Trade ID" value={String(selectedTrade.trade_id)} mono />
                <ChartMetric label="Side" value={selectedTrade.side} tone={selectedTrade.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
                <ChartMetric label="Entry" value={`${formatDateTimeShort(selectedTrade.entry_time)} @ ${formatNumber(Number(selectedTrade.entry_price))}`} />
                <ChartMetric label="Exit" value={`${formatDateTimeShort(selectedTrade.exit_time)} @ ${formatNumber(Number(selectedTrade.exit_price))}`} />
                <ChartMetric label="Final SL" value={formatNumber(Number(selectedTrade.final_sl_price))} />
                <ChartMetric label="Target" value={formatNumber(Number(selectedTrade.target_price))} />
                <ChartMetric label="Net P&L" value={money(Number(selectedTrade.net_pnl || 0))} tone={Number(selectedTrade.net_pnl || 0) >= 0 ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
              </div>
            )}
          </div>

          <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Replay summary</div>
            <div className="mt-3 grid gap-2 text-sm">
              <ChartMetric label="Trades on day" value={String(chartTrades.length)} />
              <ChartMetric label="Setups on day" value={String(chartSetups.length)} />
              <ChartMetric label="Viewport" value={viewportHint.mode === 'trade_window' ? 'Trade-focused' : 'Full day'} />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function DateSelector({
  days,
  selectedDate,
  onSelectedDateChange,
}: {
  days: any[];
  selectedDate: string;
  onSelectedDateChange: (value: string) => void;
}) {
  return (
    <select value={selectedDate} onChange={(event) => onSelectedDateChange(event.target.value)} className="control text-sm">
      {days.map((day) => <option key={day.date} value={day.date}>{day.date}</option>)}
    </select>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: () => void }) {
  return (
    <label className="flex items-center justify-between gap-3">
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={onChange} />
    </label>
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

function ChartMetric({ label, value, tone = 'text-gray-100', mono = false }: { label: string; value: string; tone?: string; mono?: boolean }) {
  return (
    <div className="rounded border border-[#1f2937] bg-[#0d1117] p-2">
      <div className="label">{label}</div>
      <div className={`mt-1 text-sm ${tone} ${mono ? 'font-mono break-all' : 'num'}`}>{value}</div>
    </div>
  );
}

function indexForTime(candles: any[], timeValue: string | null | undefined) {
  const target = parseMaybeDate(timeValue)?.getTime();
  if (!target || !candles.length) return 0;
  let bestIndex = 0;
  for (let index = 0; index < candles.length; index += 1) {
    const current = candles[index].timeMs;
    if (current === null || current === undefined) continue;
    if (current <= target) bestIndex = index;
    if (current > target) break;
  }
  return bestIndex;
}

function buildTrailingPath(
  candles: any[],
  start: number,
  candleWidth: number,
  yForPrice: (price: number) => number,
  initialSl: number,
  entryTime: string,
  exitTime: string,
  trailingMoves: any[],
  finalSl: number,
) {
  const points = [
    { time: entryTime, sl: initialSl },
    ...trailingMoves.map((move: any) => ({ time: move.time, sl: Number(move.new_sl) })),
    { time: exitTime, sl: finalSl },
  ];
  let path = '';
  let previousX = 0;
  let previousY = 0;
  points.forEach((point, index) => {
    const rawIndex = indexForTime(candles, point.time);
    const localIndex = Math.max(0, rawIndex - start);
    const x = localIndex * candleWidth + candleWidth / 2;
    const y = yForPrice(Number(point.sl));
    if (index === 0) {
      path = `M ${x} ${y}`;
    } else {
      path += ` H ${x} V ${y}`;
    }
    previousX = x;
    previousY = y;
  });
  if (!path && Number.isFinite(previousX) && Number.isFinite(previousY)) {
    path = `M ${previousX} ${previousY}`;
  }
  return path;
}

function buildTimeTicks(visibleCandles: any[], startIndex: number, candleWidth: number, desiredCount: number) {
  if (!visibleCandles.length) return [];
  const count = Math.max(2, desiredCount);
  const rawStep = Math.max(1, Math.floor((visibleCandles.length - 1) / Math.max(1, count - 1)));
  const indices = new Set<number>([0, visibleCandles.length - 1]);
  for (let index = rawStep; index < visibleCandles.length - 1; index += rawStep) {
    indices.add(index);
  }
  return Array.from(indices)
    .sort((left, right) => left - right)
    .map((visibleIndex) => {
      const candle = visibleCandles[visibleIndex];
      return {
        key: `${startIndex + visibleIndex}-${candle.time}`,
        x: visibleIndex * candleWidth + candleWidth / 2,
        label: formatAxisTime(candle.time),
      };
    });
}

function formatNumber(value: number) {
  return Number(value || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function money(value: number) {
  return `Rs ${formatNumber(value)}`;
}

function parseMaybeDate(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDateTimeShort(value: string | null | undefined) {
  const date = parseMaybeDate(value);
  if (!date) return '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function formatAxisTime(value: string | null | undefined) {
  const date = parseMaybeDate(value);
  if (!date) return '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function formatAxisDateTime(value: string | null | undefined) {
  const date = parseMaybeDate(value);
  if (!date) return '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function touchDistance(
  first: { clientX: number; clientY: number },
  second: { clientX: number; clientY: number },
) {
  return Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY);
}
