import { supabase } from './supabaseClient';
import { getPinToken } from './pinAuth';

export async function getAuthToken() {
  const { data: { session } } = await supabase.auth.getSession();
  return session?.access_token || getPinToken();
}
