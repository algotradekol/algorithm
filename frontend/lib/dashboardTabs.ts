export const ALL_DASHBOARD_TABS = [
  'Simple',
  'Filter',
  'Silver Micro',
  'Feed Health',
  'Backtest',
  'Compare',
  'History',
  'Calendar',
  'Charges',
] as const;

export type DashboardTabName = (typeof ALL_DASHBOARD_TABS)[number];

export const DASHBOARD_TAB_ROUTES: Record<DashboardTabName, string> = {
  Simple: '/simple',
  Filter: '/filter',
  'Silver Micro': '/silver',
  'Feed Health': '/feed-health',
  Backtest: '/backtest',
  Compare: '/compare',
  History: '/history',
  Calendar: '/calendar',
  Charges: '/charges',
};

export const DASHBOARD_ROUTE_TO_TAB: Record<string, DashboardTabName> = {
  '/dashboard': 'Simple',
  '/simple': 'Simple',
  '/filter': 'Filter',
  '/silver': 'Silver Micro',
  '/feed-health': 'Feed Health',
  '/backtest': 'Backtest',
  '/compare': 'Compare',
  '/history': 'History',
  '/calendar': 'Calendar',
  '/charges': 'Charges',
};

export const DASHBOARD_SLUG_TO_TAB: Record<string, DashboardTabName> = {
  simple: 'Simple',
  filter: 'Filter',
  silver: 'Silver Micro',
  'feed-health': 'Feed Health',
  backtest: 'Backtest',
  compare: 'Compare',
  history: 'History',
  calendar: 'Calendar',
  charges: 'Charges',
};

