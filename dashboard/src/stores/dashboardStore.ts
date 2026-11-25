import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { Timeframe, LayoutItem, AccountInfo } from '@/types';

interface DashboardState {
  /* Trading state */
  symbol: string;
  timeframe: Timeframe;

  /* Account */
  account: AccountInfo;

  /* Layout */
  gridLayout: LayoutItem[];
  sidebarCollapsed: boolean;

  /* Actions */
  setSymbol: (symbol: string) => void;
  setTimeframe: (tf: Timeframe) => void;
  setAccount: (account: Partial<AccountInfo>) => void;
  setGridLayout: (layout: LayoutItem[]) => void;
  toggleSidebar: () => void;
}

const DEFAULT_LAYOUT: LayoutItem[] = [
  { i: 'live-chart', x: 0, y: 0, w: 7, h: 6, minW: 4, minH: 3 },
  { i: 'agent-console', x: 7, y: 0, w: 5, h: 6, minW: 3, minH: 3 },
  { i: 'positions', x: 0, y: 6, w: 4, h: 4, minW: 3, minH: 2 },
  { i: 'order-book', x: 4, y: 6, w: 4, h: 4, minW: 3, minH: 2 },
  { i: 'risk-gauge', x: 8, y: 6, w: 4, h: 4, minW: 2, minH: 2 },
  { i: 'news-feed', x: 0, y: 10, w: 3, h: 4, minW: 2, minH: 2 },
  { i: 'trade-journal', x: 3, y: 10, w: 3, h: 4, minW: 2, minH: 2 },
  { i: 'performance', x: 6, y: 10, w: 3, h: 4, minW: 2, minH: 2 },
  { i: 'market-depth', x: 9, y: 10, w: 3, h: 4, minW: 2, minH: 2 },
  { i: 'strategy-config', x: 0, y: 14, w: 4, h: 3, minW: 2, minH: 2 },
  { i: 'alerts', x: 4, y: 14, w: 4, h: 3, minW: 2, minH: 2 },
  { i: 'system-status', x: 8, y: 14, w: 4, h: 3, minW: 2, minH: 2 },
];

export const useDashboardStore = create<DashboardState>()(
  persist(
    (set) => ({
      symbol: 'XAUUSD',
      timeframe: 'M15',

      account: {
        balance: 0,
        equity: 0,
        margin: 0,
        freeMargin: 0,
        dailyPnL: 0,
        openPositions: 0,
      },

      gridLayout: DEFAULT_LAYOUT,
      sidebarCollapsed: false,

      setSymbol: (symbol) => set({ symbol }),
      setTimeframe: (timeframe) => set({ timeframe }),
      setAccount: (account) =>
        set((s) => ({ account: { ...s.account, ...account } })),
      setGridLayout: (gridLayout) => set({ gridLayout }),
      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
    }),
    {
      name: 'nexus-dashboard',
      version: 4,
      partialize: (state) => ({
        symbol: state.symbol,
        timeframe: state.timeframe,
        gridLayout: state.gridLayout,
        sidebarCollapsed: state.sidebarCollapsed,
      }),
      migrate: (persisted: unknown) => {
        // Whitelist preferences. Old snapshots contained optimistic trading state.
        const s = (persisted ?? {}) as Partial<DashboardState>;
        return {
          symbol: s.symbol && ['XAUUSD', 'BTCUSD'].includes(s.symbol) ? s.symbol : 'XAUUSD',
          timeframe: s.timeframe ?? 'M15',
          gridLayout: s.gridLayout ?? DEFAULT_LAYOUT,
          sidebarCollapsed: s.sidebarCollapsed ?? false,
        };
      },
    }
  )
);
