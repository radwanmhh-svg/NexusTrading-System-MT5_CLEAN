'use client';

import React, { useEffect, useState, useCallback } from 'react';
import {
  Activity,
  Cpu,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  Target,
  BarChart3,
  Clock,
  Zap,
  AlertCircle,
  CheckCircle2,
  Play,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9000';

interface ModelPerformance {
  model_type: string;
  model_name: string;
  is_fitted: boolean;
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  max_drawdown: number;
  total_return: number;
  avg_rr: number;
  avg_win_pips: number;
  avg_loss_pips: number;
  expectancy: number;
  train_samples: number;
  last_trained: string;
  training_time_sec: number;
  equity_curve: number[];
  recent_signals: Array<{
    direction: string;
    confidence: number;
    sl_pips: number;
    tp_pips: number;
    symbol: string;
    timestamp: string;
    reasoning: string;
  }>;
}

interface TraderStatus {
  model_type: string;
  model_name: string;
  is_fitted: boolean;
  performance: ModelPerformance;
}

function MetricCard({
  label,
  value,
  format = 'number',
  positive,
  icon: Icon,
}: {
  label: string;
  value: number;
  format?: 'number' | 'pct' | 'pips' | 'ratio';
  positive?: boolean;
  icon?: React.ElementType;
}) {
  const formatted =
    format === 'pct'
      ? `${(value * 100).toFixed(1)}%`
      : format === 'pips'
      ? `${value.toFixed(1)}`
      : format === 'ratio'
      ? value.toFixed(2)
      : value.toFixed(4);

  return (
    <div className="flex flex-col gap-1 p-3 rounded-lg bg-bg-card border border-border-primary/20">
      <div className="flex items-center gap-1.5">
        {Icon && <Icon className="h-3 w-3 text-text-muted" />}
        <span className="text-[10px] text-text-muted font-mono uppercase">{label}</span>
      </div>
      <span
        className={cn(
          'text-lg font-mono font-bold',
          positive === undefined
            ? 'text-text-primary'
            : positive
            ? 'text-neon-green'
            : 'text-neon-red'
        )}
      >
        {formatted}
      </span>
    </div>
  );
}

function MiniEquityChart({ data, color = '#00f5ff' }: { data: number[]; color?: string }) {
  if (data.length < 2) return <div className="text-[10px] text-text-muted">No equity data</div>;

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const w = 280;
  const h = 80;

  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * h;
    return `${x},${y}`;
  });

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-20">
      <polyline
        points={points.join(' ')}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <polyline
        points={`0,${h} ${points.join(' ')} ${w},${h}`}
        fill={`${color}15`}
        stroke="none"
      />
    </svg>
  );
}

function SignalRow({
  signal,
}: {
  signal: ModelPerformance['recent_signals'][0];
}) {
  const dirColor =
    signal.direction === 'BUY'
      ? 'text-neon-green'
      : signal.direction === 'SELL'
      ? 'text-neon-red'
      : 'text-text-muted';

  return (
    <div className="flex items-center gap-2 py-1.5 border-b border-border-primary/10 last:border-0">
      <span className={cn('text-[10px] font-mono font-bold w-12', dirColor)}>
        {signal.direction}
      </span>
      <span className="text-[10px] font-mono text-neon-cyan w-16">{signal.symbol}</span>
      <span className="text-[10px] font-mono text-text-secondary">
        conf={signal.confidence.toFixed(2)}
      </span>
      <span className="text-[10px] font-mono text-neon-gold">
        SL={signal.sl_pips.toFixed(0)} TP={signal.tp_pips.toFixed(0)}
      </span>
      <span className="flex-1 text-[10px] font-mono text-text-muted truncate">
        {signal.reasoning?.slice(0, 60)}
      </span>
      <span className="text-[9px] text-text-muted shrink-0">
        {signal.timestamp ? new Date(signal.timestamp).toLocaleTimeString() : ''}
      </span>
    </div>
  );
}

function useBacktestState() {
  const [bt, setBt] = useState<any>(null);
  useEffect(() => {
    fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9000'}/api/ml/traders/economic-backtest`)
      .then(r => r.json()).then(d => setBt(d)).catch(() => {});
  }, []);
  return { bt, setBt };
}

export default function MLModelsPage() {
  const { bt, setBt } = useBacktestState();
  const [status, setStatus] = useState<TraderStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [training, setTraining] = useState(false);
  const [error, setError] = useState('');

  const fetchStatus = useCallback(async () => {
    // Abort after 8s so the panel never hangs on "Loading…" while the ML
    // service is busy retraining (its event loop blocks during training).
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    try {
      const res = await fetch(`${API}/api/ml/traders/status`, { signal: ctrl.signal });
      if (res.ok) {
        const data = await res.json();
        setStatus(data.ml);
        setError('');
      } else {
        setError('Service unavailable');
      }
    } catch {
      setError('ML service busy (retraining) — retrying…');
    } finally {
      clearTimeout(timer);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 10_000);
    return () => clearInterval(id);
  }, [fetchStatus]);

  const handleTrain = async () => {
    setTraining(true);
    try {
      await fetch(`${API}/api/ml/traders/train`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_types: ['ml'] }),
      });
    } catch {}
    setTimeout(() => {
      setTraining(false);
      fetchStatus();
    }, 3000);
  };

  const perf = status?.performance;

  return (
    <div className="p-2 sm:p-4 w-full max-w-[1800px] mx-auto space-y-3 sm:space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <Cpu className="h-5 w-5 text-neon-cyan" />
          <h1 className="text-sm font-bold text-text-primary">Machine Learning Trader</h1>
        </div>
        <span className="text-[10px] font-mono text-text-muted bg-bg-card px-2 py-0.5 rounded border border-border-primary/20">
          XGBoost + LightGBM | SL/TP Regression
        </span>
        <div className="flex-1" />
        {status?.is_fitted ? (
          <span className="flex items-center gap-1 text-[10px] text-neon-green">
            <CheckCircle2 className="h-3 w-3" /> Trained
          </span>
        ) : (
          <span className="flex items-center gap-1 text-[10px] text-neon-gold">
            <AlertCircle className="h-3 w-3" /> Not Trained
          </span>
        )}
        <button
          onClick={handleTrain}
          disabled={training}
          className={cn(
            'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-medium border transition-all',
            training
              ? 'border-border-primary text-text-muted cursor-not-allowed'
              : 'border-neon-cyan/30 text-neon-cyan bg-neon-cyan/5 hover:bg-neon-cyan/10'
          )}
        >
          {training ? (
            <RefreshCw className="h-3 w-3 animate-spin" />
          ) : (
            <Play className="h-3 w-3" />
          )}
          {training ? 'Training...' : 'Train Model'}
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-neon-red/10 border border-neon-red/20 text-[11px] text-neon-red">
          <AlertCircle className="h-3 w-3" /> {error}
        </div>
      )}

      {/* Metrics Grid */}
      {perf && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
            <MetricCard label="Win Rate" value={perf.win_rate} format="pct" positive={perf.win_rate > 0.5} icon={Target} />
            <MetricCard label="Profit Factor" value={perf.profit_factor} format="ratio" positive={perf.profit_factor > 1} icon={TrendingUp} />
            <MetricCard label="Sharpe" value={perf.sharpe_ratio} format="ratio" positive={perf.sharpe_ratio > 0} icon={BarChart3} />
            <MetricCard label="Max DD" value={perf.max_drawdown} format="pct" positive={perf.max_drawdown > -0.1} icon={TrendingDown} />
            <MetricCard label="Avg RR" value={perf.avg_rr} format="ratio" positive={perf.avg_rr > 1} icon={Zap} />
            <MetricCard label="Trades" value={perf.total_trades} icon={BarChart3} />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {/* Equity Curve */}
            <div className="rounded-xl border border-border-primary/20 bg-bg-primary p-3">
              <div className="flex items-center gap-2 mb-2">
                <TrendingUp className="h-3.5 w-3.5 text-neon-cyan" />
                <span className="text-[11px] font-mono font-semibold text-text-secondary">Equity Curve</span>
                <span className="text-[10px] text-text-muted ml-auto">
                  Return: {(perf.total_return * 100).toFixed(2)}%
                </span>
              </div>
              <MiniEquityChart data={perf.equity_curve} color="#00f5ff" />
            </div>

            {/* Model Info */}
            <div className="rounded-xl border border-border-primary/20 bg-bg-primary p-3">
              <div className="flex items-center gap-2 mb-2">
                <Cpu className="h-3.5 w-3.5 text-neon-cyan" />
                <span className="text-[11px] font-mono font-semibold text-text-secondary">Model Info</span>
              </div>
              <div className="grid grid-cols-2 gap-2 text-[11px] font-mono">
                <div className="flex justify-between">
                  <span className="text-text-muted">Direction</span>
                  <span className="text-text-primary">XGBoost (300 trees)</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">SL/TP</span>
                  <span className="text-text-primary">LightGBM Regression</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Samples</span>
                  <span className="text-neon-cyan">{perf.train_samples.toLocaleString()}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Train Time</span>
                  <span className="text-text-primary">{perf.training_time_sec.toFixed(1)}s</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Avg Win</span>
                  <span className="text-neon-green">{perf.avg_win_pips.toFixed(1)} pips</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Avg Loss</span>
                  <span className="text-neon-red">{perf.avg_loss_pips.toFixed(1)} pips</span>
                </div>
                <div className="col-span-2 flex justify-between">
                  <span className="text-text-muted">Last Trained</span>
                  <span className="text-text-secondary">
                    {perf.last_trained ? new Date(perf.last_trained).toLocaleString() : 'Never'}
                  </span>
                </div>
              </div>
            </div>
          </div>


          {/* T15.5: Backtest اقتصادي — PF بمنطق SL/TP فعلي + تكاليف */}
          <div className="rounded-xl border border-neon-gold/30 bg-neon-gold/5 p-3 mb-3">
            <div className="flex items-center gap-2 mb-2">
              <Activity className="h-3.5 w-3.5 text-neon-gold" />
              <span className="text-[11px] font-mono font-semibold text-text-secondary">Economic Backtest — PF على SL/TP فعلي بعد التكاليف</span>
              <button
                onClick={async () => {
                  setBt({ status: 'started', running: true });
                  try {
                    await fetch(`${API}/api/ml/traders/economic-backtest`, { method: 'POST' });
                    const poll = setInterval(async () => {
                      const r = await fetch(`${API}/api/ml/traders/economic-backtest`).then(x => x.json()).catch(() => null);
                      if (r && !r.running) { clearInterval(poll); setBt(r); }
                      else if (r) setBt(r);
                    }, 15000);
                  } catch { setBt({ status: 'error', running: false }); }
                }}
                disabled={bt?.running}
                className="ml-auto px-2.5 py-1 rounded border border-neon-gold/40 text-[10px] font-mono font-bold text-neon-gold hover:bg-neon-gold/10 disabled:opacity-50"
              >
                {bt?.running ? '⏳ جارٍ (عدة دقائق)...' : '▶ Run Backtest'}
              </button>
            </div>
            {bt?.status === 'ok' && bt?.result?.thresholds ? (
              <div className="grid grid-cols-3 md:grid-cols-6 gap-2 text-[11px] font-mono">
                {Object.entries(bt.result.thresholds as Record<string, Record<string, number>>).slice(-1).map(([th, m]) => (
                  <>
                    <div key="pf" className="rounded bg-bg-primary/50 p-2">
                      <div className="text-text-muted">PF (th={th})</div>
                      <div className={m.profit_factor >= 1.15 ? 'text-neon-green font-bold' : 'text-neon-red font-bold'}>{Number(m.profit_factor ?? 0).toFixed(2)}</div>
                    </div>
                    <div key="wr" className="rounded bg-bg-primary/50 p-2">
                      <div className="text-text-muted">Win Rate</div>
                      <div className="text-text-primary">{Number(m.win_rate ?? 0).toFixed(1)}%</div>
                    </div>
                    <div key="tr" className="rounded bg-bg-primary/50 p-2">
                      <div className="text-text-muted">Trades</div>
                      <div className="text-text-primary">{m.trades ?? 0}</div>
                    </div>
                    <div key="ex" className="rounded bg-bg-primary/50 p-2">
                      <div className="text-text-muted">Expectancy</div>
                      <div className={Number(m.expectancy ?? 0) >= 0 ? 'text-neon-green' : 'text-neon-red'}>${Number(m.expectancy ?? 0).toFixed(2)}</div>
                    </div>
                    <div key="dd" className="rounded bg-bg-primary/50 p-2">
                      <div className="text-text-muted">MaxDD</div>
                      <div className="text-neon-red">${Number(m.max_drawdown ?? 0).toFixed(0)}</div>
                    </div>
                    <div key="tot" className="rounded bg-bg-primary/50 p-2">
                      <div className="text-text-muted">Total P&L</div>
                      <div className={Number(m.total_pnl ?? 0) >= 0 ? 'text-neon-green' : 'text-neon-red'}>${Number(m.total_pnl ?? 0).toFixed(0)}</div>
                    </div>
                  </>
                ))}
                <div className="col-span-3 md:col-span-6 text-[10px] text-text-muted" dir="auto">
                  النتيجة من walk-forward خارج العينة على XAUUSD M5 بحاجز الترند (P9) — الحكم الاقتصادي الوحيد المعتمد (PF&gt;1.15 بعد التكاليف). {bt.result.generated_at ? `· آخر تشغيل: ${String(bt.result.generated_at).slice(0, 16).replace('T', ' ')}` : ''}
                </div>
              </div>
            ) : (
              <div className="text-[11px] text-text-muted font-mono" dir="auto">
                {bt?.running ? 'يعمل في الخلفية — النتيجة تظهر تلقائياً عند الانتهاء' : bt?.status === 'never_run' ? 'لم يُشغَّل بعد — اضغط Run (يستغرق عدة دقائق على 100k شمعة)' : bt?.status === 'started' ? 'بدأ الآن — انتظر...' : '—'}
              </div>
            )}
          </div>

          {/* Recent Signals */}
          <div className="rounded-xl border border-border-primary/20 bg-bg-primary p-3">
            <div className="flex items-center gap-2 mb-2">
              <Zap className="h-3.5 w-3.5 text-neon-gold" />
              <span className="text-[11px] font-mono font-semibold text-text-secondary">Recent Signals</span>
              <span className="text-[10px] text-text-muted ml-auto">{perf.recent_signals.length} signals</span>
            </div>
            <div className="max-h-[300px] overflow-y-auto scrollbar-hide">
              {(() => {
                // P15/T15.3: dedup — نفس الشمعة تُعاد كل دورة؛ نعرض إشارة واحدة لكل (direction,conf,sl,tp,prob)
                const seen = new Set<string>();
                const unique = perf.recent_signals.filter((sig) => {
                  const k = `${sig.direction}|${sig.confidence}|${sig.sl_pips}|${sig.tp_pips}|${sig.timestamp ?? ''}`;
                  if (seen.has(k)) return false;
                  seen.add(k);
                  return true;
                });
                if (unique.length === 0) return <div className="text-[10px] text-text-muted text-center py-4">No signals yet</div>;
                return unique.map((sig, i) => <SignalRow key={i} signal={sig} />);
              })()}
            </div>
          </div>
        </>
      )}

      {loading && !perf && (
        <div className="flex items-center justify-center py-12 text-text-muted text-xs">
          <RefreshCw className="h-4 w-4 animate-spin mr-2" /> Loading ML model status...
        </div>
      )}
    </div>
  );
}
