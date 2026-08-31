export const ALL_DASHBOARD_TABS = [
  'Simple',
  'Filter',
  'Silver Micro',
  'Silver Micro 2.0',
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
  'Silver Micro 2.0': '/silver-2',
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
  '/silver-2': 'Silver Micro 2.0',
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
  'silver-2': 'Silver Micro 2.0',
  silver2: 'Silver Micro 2.0',
  'feed-health': 'Feed Health',
  backtest: 'Backtest',
  compare: 'Compare',
  history: 'History',
  calendar: 'Calendar',
  charges: 'Charges',
};

