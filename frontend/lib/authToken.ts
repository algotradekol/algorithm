import { supabase } from './supabaseClient';
import { getPinToken } from './pinAuth';
import { getViewerToken, isViewerToken } from './viewerAuth';

export async function getAuthToken() {
  const { data: { session } } = await supabase.auth.getSession();
  return session?.access_token || getPinToken() || getViewerToken();
}

export async function getAuthContext() {
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token || getPinToken() || getViewerToken();
  return { token, isViewer: isViewerToken(token) };
}
