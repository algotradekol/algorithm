const PIN_TOKEN_KEY = 'algo-pin-access-token';

export function getPinToken() {
  if (typeof window === 'undefined') return null;
  return window.sessionStorage.getItem(PIN_TOKEN_KEY);
}

export function setPinToken(token: string) {
  window.sessionStorage.setItem(PIN_TOKEN_KEY, token);
}

export function clearPinToken() {
  if (typeof window !== 'undefined') {
    window.sessionStorage.removeItem(PIN_TOKEN_KEY);
  }
}
