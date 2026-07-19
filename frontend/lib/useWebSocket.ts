import { useCallback, useEffect, useRef } from 'react';
import { getAuthToken } from './authToken';

const WS_URL = process.env.NEXT_PUBLIC_API_URL
  ?.replace('https://', 'wss://')
  ?.replace('http://', 'ws://');

export type WebSocketState = 'connected' | 'reconnecting' | 'offline';

export function useWebSocket(
  onMessage: (data: any) => void,
  enabled = true,
  onStatus?: (status: WebSocketState) => void,
) {
  const ws = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<any>(null);
  const retryCount = useRef(0);
  const shouldReconnect = useRef(false);

  const connect = useCallback(async () => {
    if (!shouldReconnect.current) return;
    if (!WS_URL) return;
    const token = await getAuthToken();
    if (!token) return;

    const url = `${WS_URL}/ws?token=${encodeURIComponent(token)}`;
    ws.current = new WebSocket(url);

    ws.current.onopen = () => {
      retryCount.current = 0;
      onStatus?.('connected');
    };

    ws.current.onmessage = (e) => {
      try { onMessage(JSON.parse(e.data)); }
      catch {}
    };

    ws.current.onclose = () => {
      if (!shouldReconnect.current) return;
      retryCount.current += 1;
      onStatus?.(retryCount.current > 3 ? 'offline' : 'reconnecting');
      reconnectTimer.current = setTimeout(connect, 3000);
    };

    ws.current.onerror = () => {
      ws.current?.close();
    };
  }, [onMessage, onStatus]);

  useEffect(() => {
    if (!enabled) return;
    shouldReconnect.current = true;
    connect();
    return () => {
      shouldReconnect.current = false;
      clearTimeout(reconnectTimer.current);
      ws.current?.close();
    };
  }, [connect, enabled]);
}
