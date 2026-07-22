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
  const heartbeatTimer = useRef<any>(null);
  const retryCount = useRef(0);
  const shouldReconnect = useRef(false);
  const connecting = useRef(false);

  function clearHeartbeat() {
    clearInterval(heartbeatTimer.current);
    heartbeatTimer.current = null;
  }

  const connect = useCallback(async () => {
    if (!shouldReconnect.current) return;
    if (!WS_URL) return;
    if (connecting.current || ws.current?.readyState === WebSocket.OPEN || ws.current?.readyState === WebSocket.CONNECTING) return;

    connecting.current = true;
    const token = await getAuthToken();
    if (!token) {
      connecting.current = false;
      return;
    }

    ws.current = new WebSocket(`${WS_URL}/ws`);

    ws.current.onopen = () => {
      connecting.current = false;
      retryCount.current = 0;
      // Keep JWTs out of proxy/request logs; the backend validates this as
      // the first WebSocket message before registering the connection.
      ws.current?.send(JSON.stringify({ token }));
      onStatus?.('connected');
      clearHeartbeat();
      heartbeatTimer.current = setInterval(() => {
        if (ws.current?.readyState === WebSocket.OPEN) {
          ws.current.send('ping');
        }
      }, 20_000);
    };

    ws.current.onmessage = (e) => {
      try { onMessage(JSON.parse(e.data)); }
      catch {}
    };

    ws.current.onclose = () => {
      connecting.current = false;
      clearHeartbeat();
      if (!shouldReconnect.current) return;
      retryCount.current += 1;
      onStatus?.(retryCount.current > 3 ? 'offline' : 'reconnecting');
      reconnectTimer.current = setTimeout(connect, 3000);
    };

    ws.current.onerror = () => {
      connecting.current = false;
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
      clearHeartbeat();
      ws.current?.close();
    };
  }, [connect, enabled]);
}
