import { notFound } from 'next/navigation';
import { DashboardPage } from '../../dashboard/page';
import { DASHBOARD_SLUG_TO_TAB } from '../../../lib/dashboardTabs';

export default function DashboardTabRoute({
  params,
}: {
  params: { tab: string };
}) {
  const slug = String(params?.tab || '').trim().toLowerCase();
  if (!DASHBOARD_SLUG_TO_TAB[slug]) notFound();
  return <DashboardPage />;
}

