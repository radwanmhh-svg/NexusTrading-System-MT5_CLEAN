'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  BarChart3,
  TrendingUp,
  TrendingDown,
  Percent,
  DollarSign,
  Target,
  Timer,
  Activity,
  RefreshCw,
  AlertCircle,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9000';

type AnalyticsTab = 'overview' | 'sessions' | 'symbols' | 'drawdown';

const TABS: { value: AnalyticsTab; label: string }[] = [
  { value: 'overview', label: 'Overview' },
  { value: 'sessions', label: 'By Session' },
  { value: 'symbols', label: 'By Symbol' },
  { value: 'drawdown', label: 'Drawdown' },
];

interface PerfSummary {
  total_pnl: number;
  total_trades: number;
  avg_win: number;
  avg_loss: number;
  win_rate: number;
  profit_factor: number;
}

interface CurvePoint {
  closed_at: string;
  pnl_usd: number;
  cumulative_pnl: number;
}

interface GroupPerf {
  group: string;
  total_pnl: number;
  trades: number;
  win_rate: number;
  avg_rr: number;
}

function fmtMoney(v: number): string {
  return `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function EquityChart({ data }: { data: CurvePoint[] }) {
  if (!data || data.length < 2)
    return (
      <div className="h-64 flex items-center justify-center border border-dashed border-border-primary rounded-lg">
        <span className="text-text-muted text-sm font-mono">No closed trades yet — equity curve appears after the first session</span>
      </div>
    );
  const vals = data.map(d => d.cumulative_pnl);
  const min = Math.min(0, ...vals);
  const max = Math.max(...vals, 0.01);
  const w = 800, h = 240, pad = 8;
  const pts = data.map((d, i) => {
    const x = pad + (i / (data.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((d.cumulative_pnl - min) / (max - min)) * (h - 2 * pad);
    return `${x},${y}`;
  });
  const zeroY = h - pad - ((0 - min) / (max - min)) * (h - 2 * pad);
  const positive = (vals[vals.length - 1] ?? 0) >= 0;
  const color = positive ? 'var(--neon-green)' : 'var(--neon-red)';
  return (
    <div className="h-64">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-full">
        <line x1={pad} y1={zeroY} x2={w - pad} y2={zeroY} stroke="var(--border-primary, #333)" strokeDasharray="4 4" strokeWidth="1" />
        <polyline points={pts.join(' ')} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function DailyBars({ data }: { data: { date: string; pnl: number; trades: number }[] }) {
  if (!data || data.length === 0)
    return <div className="h-48 flex items-center justify-center border border-dashed border-border-primary rounded-lg"><span className="text-text-muted text-xs font-mono">Histogram — awaiting closed trades</span></div>;
  const maxAbs = Math.max(...data.map(d => Math.abs(d.pnl)), 0.01);
  return (
    <div className="h-48 flex items-end gap-1 overflow-x-auto">
      {data.slice(0, 30).reverse().map(d => (
        <div key={d.date} className="flex flex-col items-center gap-0.5 min-w-[16px]" title={`${d.date}: ${fmtMoney(d.pnl)} (${d.trades} trades)`}>
          <div
            className={cn('w-3 rounded-t', d.pnl >= 0 ? 'bg-neon-green/70' : 'bg-neon-red/70')}
            style={{ height: `${Math.max(4, (Math.abs(d.pnl) / maxAbs) * 150)}px` }}
          />
          <span className="text-[8px] text-text-muted font-mono">{d.date.slice(5)}</span>
        </div>
      ))}
    </div>
  );
}

function GroupTable({ rows, title, groupLabel }: { rows: GroupPerf[]; title: string; groupLabel: string }) {
  return (
    <div className="neon-card p-6">
      <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-4">{title}</h3>
      {(!rows || rows.length === 0) ? (
        <div className="h-48 flex items-center justify-center border border-dashed border-border-primary rounded-lg">
          <span className="text-text-muted text-sm font-mono">No closed trades yet</span>
        </div>
      ) : (
        <table className="w-full text-xs font-mono">
          <thead>
            <tr className="text-text-muted border-b border-border-primary">
              <th className="text-left py-2">{groupLabel}</th>
              <th className="text-right py-2">Net P&L</th>
              <th className="text-right py-2">Trades</th>
              <th className="text-right py-2">Win Rate</th>
              <th className="text-right py-2">Avg R:R</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.group} className="border-b border-border-primary/40">
                <td className="py-2 text-neon-cyan font-bold">{r.group || '—'}</td>
                <td className={cn('py-2 text-right font-bold', r.total_pnl >= 0 ? 'text-neon-green' : 'text-neon-red')}>{fmtMoney(r.total_pnl)}</td>
                <td className="py-2 text-right text-text-secondary">{r.trades}</td>
                <td className="py-2 text-right text-text-secondary">{r.win_rate.toFixed(1)}%</td>
                <td className="py-2 text-right text-text-muted">{r.avg_rr.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function AnalyticsPage() {
  const [activeTab, setActiveTab] = useState<AnalyticsTab>('overview');
  const [summary, setSummary] = useState<PerfSummary | null>(null);
  const [curve, setCurve] = useState<CurvePoint[]>([]);
  const [daily, setDaily] = useState<{ date: string; pnl: number; trades: number; win_rate: number }[]>([]);
  const [sessions, setSessions] = useState<GroupPerf[]>([]);
  const [symbols, setSymbols] = useState<GroupPerf[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, c, d, ss, sy] = await Promise.all([
        fetch(`${API}/api/performance`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/api/performance/equity-curve?limit=500`).then(r => r.ok ? r.json() : []),
        fetch(`${API}/api/performance/daily`).then(r => r.ok ? r.json() : []),
        fetch(`${API}/api/performance/by-session`).then(r => r.ok ? r.json() : []),
        fetch(`${API}/api/performance/by-symbol`).then(r => r.ok ? r.json() : []),
      ]);
      setSummary(s);
      setCurve(Array.isArray(c) ? c : []);
      setDaily(Array.isArray(d) ? d : []);
      setSessions(Array.isArray(ss) ? ss : []);
      setSymbols(Array.isArray(sy) ? sy : []);
      setError('');
    } catch {
      setError('gateway unreachable — retrying on next refresh');
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const s: PerfSummary = summary ?? {
    total_pnl: 0, total_trades: 0, avg_win: 0, avg_loss: 0, win_rate: 0, profit_factor: 0,
  };
  // Expectancy & recovery derived from the summary the API provides.
  const expectancy = s.total_trades > 0 ? s.total_pnl / s.total_trades : 0;
  // Drawdown analysis from the equity curve
  let maxDD = 0, peak = 0;
  for (const p of curve) {
    peak = Math.max(peak, p.cumulative_pnl);
    maxDD = Math.min(maxDD, p.cumulative_pnl - peak);
  }

  const METRICS = [
    { label: 'Total P&L', value: fmtMoney(s.total_pnl), icon: DollarSign, color: 'var(--neon-green)', positive: s.total_pnl >= 0 },
    { label: 'Win Rate', value: `${(s.win_rate * 100).toFixed(1)}%`, icon: Target, color: 'var(--neon-cyan)' },
    { label: 'Profit Factor', value: s.profit_factor.toFixed(2), icon: TrendingUp, color: 'var(--neon-purple)', positive: s.profit_factor >= 1 },
    { label: 'Max Drawdown', value: fmtMoney(maxDD), icon: TrendingDown, color: 'var(--neon-red)', positive: maxDD >= -0.1 },
    { label: 'Total Trades', value: String(s.total_trades), icon: Activity, color: 'var(--neon-gold)' },
    { label: 'Avg Win / Loss', value: `${fmtMoney(s.avg_win)} / ${fmtMoney(s.avg_loss)}`, icon: Percent, color: 'var(--neon-blue)' },
    { label: 'Expectancy / Trade', value: fmtMoney(expectancy), icon: BarChart3, color: 'var(--neon-orange)', positive: expectancy >= 0 },
    { label: 'Avg R:R', value: sessions.length ? (sessions.reduce((a, r) => a + r.avg_rr, 0) / sessions.length).toFixed(2) : '—', icon: Timer, color: 'var(--neon-pink)' },
  ];

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <BarChart3 className="h-6 w-6 text-neon-cyan" />
          <div>
            <h1 className="text-xl font-bold text-text-primary">Analytics</h1>
            <p className="text-[10px] text-text-muted font-mono">live from /api/performance — closed trades only</p>
          </div>
        </div>
        <button onClick={load} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border-primary text-xs text-text-muted hover:text-text-primary hover:border-neon-cyan/40">
          <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} /> Refresh
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-neon-red/10 border border-neon-red/20 text-[11px] text-neon-red">
          <AlertCircle className="h-3.5 w-3.5" /> {error}
        </div>
      )}

      {/* P15/T15.4: تحذير حجم العينة — أي مقياس من <30 صفقة لا يدل على أداء */}
      {!loading && s.total_trades < 30 && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-neon-gold/10 border border-neon-gold/30 text-[11px] text-neon-gold" dir="auto">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          <span className="font-bold">
            ⚠ عينة غير كافية: كل المقاييس أعلاه محسوبة من {s.total_trades} صفقة فقط (&lt;30) — إحصائياً لا معنى لها (PF 87 من 3 صفقات مثال) — لا تُستخدم للقرار
          </span>
        </div>
      )}

      {/* Tabs */}
      <div className="flex items-center gap-1 bg-bg-card rounded-lg p-1 border border-border-primary w-fit">
        {TABS.map((tab) => (
          <button
            key={tab.value}
            onClick={() => setActiveTab(tab.value)}
            className={cn(
              'px-4 py-2 text-sm font-medium rounded-md transition-all',
              activeTab === tab.value
                ? 'bg-neon-purple/15 text-neon-purple'
                : 'text-text-muted hover:text-text-secondary'
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'overview' && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {METRICS.map((m) => {
              const Icon = m.icon;
              return (
                <div key={m.label} className="neon-card p-4 group">
                  <div className="flex items-center justify-between mb-3">
                    <Icon className="h-5 w-5 opacity-70 group-hover:opacity-100 transition-opacity" style={{ color: m.color }} />
                  </div>
                  <p className="text-xs text-text-muted mb-1">{m.label}</p>
                  <p className={cn('text-xl font-bold font-mono',
                    m.positive === undefined ? 'text-text-primary' : m.positive ? 'text-neon-green' : 'text-neon-red')}>
                    {m.value}
                  </p>
                </div>
              );
            })}
          </div>

          <div className="neon-card p-6">
            <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-4">
              Equity Curve (cumulative closed P&L)
            </h3>
            <EquityChart data={curve} />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="neon-card p-6">
              <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-4">
                Daily P&L (last 30 days)
              </h3>
              <DailyBars data={daily} />
            </div>
            <div className="neon-card p-6">
              <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-4">
                Win/Loss by Day
              </h3>
              {daily.length === 0 ? (
                <div className="h-48 flex items-center justify-center border border-dashed border-border-primary rounded-lg">
                  <span className="text-text-muted text-xs font-mono">Heatmap — awaiting closed trades</span>
                </div>
              ) : (
                <div className="space-y-1.5 max-h-48 overflow-y-auto">
                  {daily.slice(0, 14).map(d => (
                    <div key={d.date} className="flex items-center gap-2 text-[10px] font-mono">
                      <span className="text-text-muted w-16">{d.date.slice(5)}</span>
                      <div className="flex-1 h-2 bg-bg-primary rounded-full overflow-hidden">
                        <div
                          className={cn('h-full rounded-full', d.win_rate >= 50 ? 'bg-neon-green/60' : 'bg-neon-red/60')}
                          style={{ width: `${d.win_rate}%` }}
                        />
                      </div>
                      <span className={cn('w-10 text-right', d.win_rate >= 50 ? 'text-neon-green' : 'text-neon-red')}>{d.win_rate.toFixed(0)}%</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {activeTab === 'sessions' && <GroupTable rows={sessions} title="Performance by Session" groupLabel="Session" />}
      {activeTab === 'symbols' && <GroupTable rows={symbols} title="Performance by Symbol" groupLabel="Symbol" />}

      {activeTab === 'drawdown' && (
        <div className="neon-card p-6">
          <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-4">
            Drawdown from Peak
          </h3>
          {curve.length < 2 ? (
            <div className="h-96 flex items-center justify-center border border-dashed border-border-primary rounded-lg">
              <span className="text-text-muted text-sm font-mono">Drawdown curve appears after closed trades accumulate</span>
            </div>
          ) : (
            (() => {
              const pts: string[] = [];
              const w = 800, h = 240, pad = 8;
              let peak = 0;
              const dds: number[] = [];
              for (const p of curve) {
                peak = Math.max(peak, p.cumulative_pnl);
                dds.push(p.cumulative_pnl - peak);
              }
              const minDD = Math.min(...dds, -0.01);
              curve.forEach((p, i) => {
                const x = pad + (i / (curve.length - 1)) * (w - 2 * pad);
                const y = pad + (Math.abs(dds[i]) / Math.abs(minDD)) * (h - 2 * pad);
                pts.push(`${x},${y}`);
              });
              return (
                <div className="h-64">
                  <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-full">
                    <polygon points={`${pad},${pad} ${pts.join(' ')} ${w - pad},${pad}`} fill="rgba(255,34,85,0.15)" stroke="none" />
                    <polyline points={pts.join(' ')} fill="none" stroke="var(--neon-red)" strokeWidth="1.5" />
                    <text x={pad + 4} y={pad + 14} fill="var(--neon-red)" fontSize="12" fontFamily="monospace">
                      Max DD: {fmtMoney(maxDD)}
                    </text>
                  </svg>
                </div>
              );
            })()
          )}
        </div>
      )}
    </div>
  );
}
