'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { supabase } from '../lib/supabaseClient';
import { getPinToken } from '../lib/pinAuth';
import { getViewerToken } from '../lib/viewerAuth';

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      router.replace(session || getPinToken() || getViewerToken() ? '/delta' : '/login');
    });
  }, [router]);
  return null;
}
