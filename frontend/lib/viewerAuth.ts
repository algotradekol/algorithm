const VIEWER_TOKEN_KEY = 'algo-viewer-access-token';

export function getViewerToken() {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(VIEWER_TOKEN_KEY);
}

export function setViewerToken(token: string) {
  window.localStorage.setItem(VIEWER_TOKEN_KEY, token);
}

export function clearViewerToken() {
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(VIEWER_TOKEN_KEY);
  }
}

export function decodeJwtPayload(token: string | null): Record<string, any> | null {
  if (!token) return null;
  try {
    const [, payload] = token.split('.');
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
    return JSON.parse(atob(padded));
  } catch {
    return null;
  }
}

export function isViewerToken(token: string | null) {
  const payload = decodeJwtPayload(token);
  if (!payload) return false;
  if (payload.exp && payload.exp * 1000 <= Date.now()) {
    clearViewerToken();
    return false;
  }
  return payload.role === 'viewer' || payload.login_method === 'viewer_invite';
}
