'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  Zap,
  LayoutDashboard,
  History,
  BrainCircuit,
  Settings,
  ListChecks,
  Wallet,
  Cpu,
  Trophy,
  Database as Database2,
  Map as RoadmapIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { apiClient } from '@/lib/api-client';
import { useDashboardStore } from '@/stores/dashboardStore';
import type { Timeframe } from '@/types';
import { refreshControls, requestMode, useTradeControls, type ExecutionMode } from '@/stores/tradeControls';
import { TradeControlNotice } from '@/components/trade-controls/TradeControlNotice';
import { NotificationBell } from '@/components/ui/NotificationBell';

function GatewaySettingsHydrator() {

  useEffect(() => {
    void refreshControls();
    const timer = window.setInterval(refreshControls, 5000);
    window.addEventListener('focus', refreshControls);

    let cancelled = false;
    (async () => {
      try {
        const [settingsRes, positionsRes] = await Promise.all([
          apiClient.getSettings(),
          apiClient.getPositionsSnapshot().catch(() => null),
        ]);
        if (cancelled) return;

        const d = settingsRes.data;
        const sym = d.default_symbol;
        if (typeof sym === 'string' && sym.length >= 4) {
          useDashboardStore.getState().setSymbol(sym.toUpperCase().replace(/\//g, ''));
        }
        const tf = d.default_timeframe;
        if (typeof tf === 'string') {
          useDashboardStore.getState().setTimeframe(tf as Timeframe);
        }
        if (positionsRes?.data?.account) {
          const acc = positionsRes.data.account;
          useDashboardStore.getState().setAccount({
            balance: acc.balance ?? undefined,
            equity: acc.equity ?? undefined,
            margin: acc.margin ?? undefined,
            freeMargin: acc.free_margin ?? undefined,
          });
        }
      } catch {
        /* gateway offline — keep persisted state */
      }
    })();

    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener('focus', refreshControls);
    };
  }, []);

  return null;
}

const NAV_ITEMS = [
  { href: '/dashboard',                       label: 'Trading',    icon: LayoutDashboard },
  { href: '/dashboard/strategies',             label: 'Strategies', icon: BrainCircuit },
  { href: '/dashboard/models',                 label: 'Models',     icon: Cpu },
  { href: '/dashboard/model-comparison',       label: 'Compare',    icon: Trophy },
  { href: '/dashboard/history',                label: 'History',    icon: History },
  { href: '/dashboard/data-import',            label: 'Data Import', icon: Database2 },
  { href: '/dashboard/roadmap',                label: 'Roadmap',    icon: RoadmapIcon },
  { href: '/dashboard/settings',               label: 'Settings',   icon: Settings },
  { href: '/dashboard/capabilities',           label: 'Capabilities', icon: ListChecks },
];

function ModeToggle() {
  const { snapshot, busy } = useTradeControls();
  const [liveDialog, setLiveDialog] = useState(false);
  const [pin, setPin] = useState('');
  return (
    <>
      <select aria-label="Execution mode" disabled={busy || !snapshot} value={snapshot?.mode ?? ''}
        className="rounded border border-border-primary bg-bg-secondary px-2 py-1 text-xs font-mono text-text-primary"
        onChange={(e) => {
          const mode = e.currentTarget.value as ExecutionMode;
          if (mode === 'live') { setPin(''); setLiveDialog(true); }
          else void requestMode(mode);
        }}>
        <option value="" disabled>{busy ? 'Confirming…' : 'Unknown'}</option>
        <option value="shadow">SHADOW</option><option value="paper">PAPER</option>
        <option value="backtest">BACKTEST</option><option value="live">LIVE</option>
      </select>
      {liveDialog && (
        <form role="dialog" aria-label="Confirm live mode" onSubmit={async e => {
          e.preventDefault();
          const submitted = pin;
          setPin(''); setLiveDialog(false);
          await requestMode('live', submitted);
        }} className="fixed top-14 right-4 z-50 w-72 space-y-3 rounded border border-neon-red bg-bg-card p-4 text-xs">
          <p>LIVE uses real funds. A mode change leaves AUTO off. Enter the gateway PIN to request this mode.</p>
          <input autoFocus type="password" required autoComplete="off" aria-label="Gateway PIN" value={pin}
            className="w-full bg-bg-input p-2" onChange={e => setPin(e.target.value)} />
          <button type="button" className="mr-4" onClick={() => { setPin(''); setLiveDialog(false); }}>Cancel</button>
          <button type="submit" className="text-neon-red" disabled={!pin || busy}>Request LIVE</button>
        </form>
      )}
    </>
  );
}

function BalanceDisplay() {
  const balance = useDashboardStore((s) => s.account.balance);
  const equity = useDashboardStore((s) => s.account.equity);
  const dailyPnL = useDashboardStore((s) => s.account.dailyPnL);

  const fmt = (n: number) =>
    n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });

  return (
    <div className="hidden md:flex items-center gap-2 lg:gap-3 px-2 lg:px-3 py-1 rounded-md bg-bg-card border border-border-primary/30">
      <div className="flex items-center gap-1.5">
        <Wallet className="h-3 w-3 text-neon-gold" />
        <span className="text-[10px] text-text-muted font-mono">BAL</span>
        <span className="text-[11px] font-mono font-bold text-neon-gold">{fmt(balance)}</span>
      </div>
      {dailyPnL !== 0 && (
        <>
          <div className="h-3 w-px bg-border-primary" />
          <span className={cn(
            'text-[11px] font-mono font-bold',
            dailyPnL >= 0 ? 'text-neon-green' : 'text-neon-red'
          )}>
            {dailyPnL >= 0 ? '+' : ''}{fmt(dailyPnL)}
          </span>
        </>
      )}
      {equity !== balance && (
        <>
          <div className="h-3 w-px bg-border-primary" />
          <div className="flex items-center gap-1">
            <span className="text-[10px] text-text-muted font-mono">EQ</span>
            <span className="text-[11px] font-mono text-text-secondary">{fmt(equity)}</span>
          </div>
        </>
      )}
    </div>
  );
}

function LiveModeBanner() {
  const live = useTradeControls(s => s.snapshot?.mode === 'live');
  if (!live) return null;
  return (
    <div className="h-0.5 w-full bg-gradient-to-r from-transparent via-neon-red/60 to-transparent animate-pulse shrink-0" />
  );
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const snapshot = useTradeControls(s => s.snapshot);

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <GatewaySettingsHydrator />

      <TradeControlNotice />

      {/* Top Nav Bar */}
      <header className="flex items-center gap-1.5 sm:gap-2 px-2 sm:px-4 h-10 border-b border-border-primary bg-bg-secondary/90 backdrop-blur-sm shrink-0 z-20">
        {/* Brand */}
        <div className="flex items-center gap-2 mr-3 shrink-0">
          <Zap className="h-4 w-4 text-neon-green" />
          <span className="text-xs font-black tracking-[0.2em] gradient-text hidden sm:inline select-none">
            NEXUS
          </span>
          <span className="hidden sm:inline px-1.5 py-0.5 rounded text-[8px] font-mono font-bold bg-yellow-400/10 border border-yellow-400/30 text-yellow-400 tracking-wider select-none">
            {snapshot?.mode.toUpperCase() ?? "UNKNOWN"}
          </span>
        </div>

        <div className="h-4 w-px bg-border-primary shrink-0" />

        {/* Nav links */}
        <nav className="flex items-center gap-0.5 flex-1 overflow-x-auto scrollbar-hide">
          {NAV_ITEMS.map((item) => {
            const isActive =
              item.href === '/dashboard'
                ? pathname === '/dashboard'
                : pathname.startsWith(item.href);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-medium whitespace-nowrap transition-all relative',
                  isActive
                    ? 'bg-neon-purple/15 text-neon-purple'
                    : 'text-text-muted hover:text-text-secondary hover:bg-bg-tertiary/50'
                )}
              >
                <Icon className="h-3 w-3" />
                <span className="hidden sm:inline">{item.label}</span>
                {isActive && (
                  <span className="absolute bottom-0 left-1 right-1 h-px rounded-full bg-neon-purple/60" />
                )}
              </Link>
            );
          })}
        </nav>

        {/* Right controls */}
        <div className="flex items-center gap-1.5 shrink-0">
          <BalanceDisplay />

          <div className="h-4 w-px bg-border-primary" />

          <ModeToggle />

          <div className="h-4 w-px bg-border-primary" />

          <NotificationBell />
          {/* T9: زر LogOut أُزيل — لا جلسات محلية (قرار: إخفاء) */}
        </div>
      </header>

      {/* Live mode indicator line */}
      <LiveModeBanner />

      <main className="flex-1 min-h-0 overflow-auto">{children}</main>
    </div>
  );
}
