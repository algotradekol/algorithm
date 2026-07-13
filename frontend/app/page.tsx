'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { supabase } from '../lib/supabaseClient';
import { getPinToken } from '../lib/pinAuth';

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      router.replace(session || getPinToken() ? '/dashboard' : '/login');
    });
  }, [router]);
  return null;
}
