'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import {
  Database, Download, RefreshCw, Calendar, CheckCircle2, Loader2,
  AlertTriangle, Clock, HardDriveDownload,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9000';
const DATA_API = process.env.NEXT_PUBLIC_DATA_URL || 'http://localhost:9001';

const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'];
const SYMBOLS = ['XAUUSD', 'BTCUSD'];

interface TfRecord {
  last_ts?: string;
  bars_total?: number;
  last_run_new?: number;
  mode?: string;
  updated_at?: string;
  error?: string;
}

interface Registry {
  symbol?: string;
  timeframes?: Record<string, TfRecord>;
}

interface Progress {
  symbol?: string;
  status?: string;
  started_at?: string;
  finished_at?: string;
  timeframes?: string[];
  current?: string | null;
  done?: string[];
  last_error?: string | null;
  error?: string | null;
  task_running?: boolean;
  registry?: Record<string, TfRecord>;
  db_timeframes?: Record<string, { rows: number; last_ts: string }>;
  db_total_rows?: number;
}

export default function DataImportPage() {
  const [symbol, setSymbol] = useState('XAUUSD');
  const [selectedTfs, setSelectedTfs] = useState<string[]>(['M5', 'M15', 'H1', 'H4', 'D1']);
  const [startDate, setStartDate] = useState('2024-01-01');
  const [progress, setProgress] = useState<Progress | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async (sym: string) => {
    try {
      const res = await fetch(`${DATA_API}/api/data/import/progress/${sym}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setProgress(await res.json());
    } catch {
      // service warming up — keep the previous snapshot
    }
  }, []);

  useEffect(() => {
    load(symbol);
  }, [symbol, load]);

  useEffect(() => {
    if (progress?.status === 'running' || progress?.task_running) {
      if (!pollRef.current) {
        pollRef.current = setInterval(() => load(symbol), 3000);
      }
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    };
  }, [progress, symbol, load]);

  const startImport = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch(`${DATA_API}/api/data/import/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol,
          timeframes: selectedTfs.length ? selectedTfs : undefined,
          start_date: startDate || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setMessage(data.status === 'already_running'
        ? 'استيراد جارٍ بالفعل لهذا الرمز'
        : 'بدأ الاستيراد التزايدي — يُحمَّل فقط ما هو أحدث من آخر تحديث');
      setTimeout(() => load(symbol), 1000);
    } catch (e) {
      setMessage(`خطأ: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const toggleTf = (tf: string) =>
    setSelectedTfs((prev) => (prev.includes(tf) ? prev.filter((t) => t !== tf) : [...prev, tf]));

  const registry: Record<string, TfRecord> = progress?.registry || {};
  const running = progress?.status === 'running' || progress?.task_running;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="h-10 w-10 rounded-lg bg-neon-green/10 border border-neon-green/30 flex items-center justify-center">
          <Database className="h-5 w-5 text-neon-green" />
        </div>
        <div>
          <h1 className="text-lg font-bold text-text-primary">استيراد البيانات التاريخية الذكي</h1>
          <p className="text-xs text-text-muted">
            استيراد تزايدي: يُحمَّل فقط ما هو أحدث من آخر تحديث — بدون إعادة تحميل نفس الشموع في كل تشغيل
          </p>
        </div>
        <button
          onClick={() => load(symbol)}
          className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border-primary text-xs text-text-muted hover:text-text-primary hover:border-neon-green/40 transition-colors"
        >
          <RefreshCw className="h-3.5 w-3.5" /> تحديث
        </button>
      </div>

      {/* Controls */}
      <div className="rounded-xl border border-border-primary bg-bg-card p-5 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs font-semibold text-text-muted mb-1.5">الرمز</label>
            <select
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              className="w-full rounded-lg bg-bg-primary border border-border-primary px-3 py-2 text-sm text-text-primary"
            >
              {SYMBOLS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold text-text-muted mb-1.5">
              <span className="inline-flex items-center gap-1"><Calendar className="h-3 w-3" /> تاريخ تأسيس البيانات (أول استيراد فقط)</span>
            </label>
            <input
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="w-full rounded-lg bg-bg-primary border border-border-primary px-3 py-2 text-sm text-text-primary"
            />
            <p className="mt-1 text-[10px] text-text-muted">يُستخدم فقط إذا لم يكن لهذا الفريم سجل سابق</p>
          </div>
          <div>
            <label className="block text-xs font-semibold text-text-muted mb-1.5">الشريط الزمني حتى اليوم</label>
            <div className="rounded-lg bg-bg-primary border border-border-primary px-3 py-2 text-sm text-neon-green flex items-center gap-2">
              <Clock className="h-4 w-4" /> {new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC
            </div>
          </div>
        </div>

        <div>
          <label className="block text-xs font-semibold text-text-muted mb-2">الفريمات الزمنية</label>
          <div className="flex flex-wrap gap-2">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                onClick={() => toggleTf(tf)}
                className={cn(
                  'px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors',
                  selectedTfs.includes(tf)
                    ? 'bg-neon-green/10 border-neon-green/50 text-neon-green'
                    : 'bg-bg-primary border-border-primary text-text-muted hover:text-text-primary',
                )}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={startImport}
            disabled={busy || running}
            className={cn(
              'flex items-center gap-2 px-5 py-2.5 rounded-lg font-bold text-sm transition-colors',
              busy || running
                ? 'bg-bg-primary border border-border-primary text-text-muted cursor-not-allowed'
                : 'bg-neon-green/15 border border-neon-green/50 text-neon-green hover:bg-neon-green/25',
            )}
          >
            {busy || running
              ? <Loader2 className="h-4 w-4 animate-spin" />
              : <Download className="h-4 w-4" />}
            {running ? 'جارٍ الاستيراد…' : 'بدء الاستيراد / التحديث'}
          </button>
          {message && <span className="text-xs text-text-muted">{message}</span>}
        </div>
      </div>

      {/* Progress banner */}
      {progress?.status === 'running' && (
        <div className="rounded-xl border border-neon-yellow/40 bg-neon-yellow/5 p-4 flex items-center gap-3">
          <Loader2 className="h-4 w-4 animate-spin text-neon-yellow" />
          <span className="text-sm text-neon-yellow">
            استيراد جارٍ — الفريم الحالي: {progress.current || '…'}
            {progress.done?.length ? ` · اكتمل: ${progress.done.join(', ')}` : ''}
          </span>
        </div>
      )}
      {progress?.last_error && (
        <div className="rounded-xl border border-neon-red/40 bg-neon-red/5 p-4 flex items-center gap-3">
          <AlertTriangle className="h-4 w-4 text-neon-red" />
          <span className="text-sm text-neon-red">{progress.last_error}</span>
        </div>
      )}

      {/* Registry table */}
      <div className="rounded-xl border border-border-primary bg-bg-card overflow-hidden">
        <div className="px-5 py-3 border-b border-border-primary flex items-center gap-2">
          <HardDriveDownload className="h-4 w-4 text-neon-green" />
          <h2 className="text-sm font-bold text-text-primary">سجل البيانات — آخر تحديث لكل فريم ({symbol})</h2>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-text-muted border-b border-border-primary">
              <th className="text-right px-5 py-2.5 font-semibold">الفريم</th>
              <th className="text-right px-5 py-2.5 font-semibold">آخر شمعة مستوردة</th>
              <th className="text-right px-5 py-2.5 font-semibold">إجمالي الشموع</th>
              <th className="text-right px-5 py-2.5 font-semibold">الجديد في آخر تشغيل</th>
              <th className="text-right px-5 py-2.5 font-semibold">آخر تحديث</th>
              <th className="text-right px-5 py-2.5 font-semibold">الحالة</th>
            </tr>
          </thead>
          <tbody>
            {TIMEFRAMES.map((tf) => {
              const rec: TfRecord | undefined = registry[tf];
              // T15.6: القاعدة الفعلية هي مصدر الحقيقة — السجل (Redis) يُكمَّل فقط
              const db = progress?.db_timeframes?.[tf];
              const lastTs = rec?.last_ts ?? db?.last_ts;
              // FIX (2026-11-22): القاعدة الفعلية (db_rows) هي مصدر الحقيقة —
              // bars_total في السجل عدّاد جلسة MT5 يبدأ من الصفر ويعطي رقمًا مضلّلًا.
              const rows = db?.rows ?? rec?.bars_total;
              const lastRunNew = rec?.last_run_new;
              const err = rec?.error;
              return (
                <tr key={tf} className="border-b border-border-primary/50 hover:bg-bg-primary/50">
                  <td className="px-5 py-2.5 font-bold text-text-primary">{tf}</td>
                  <td className="px-5 py-2.5 text-text-muted font-mono text-xs" dir="ltr">
                    {lastTs ? String(lastTs).slice(0, 16).replace('T', ' ') : '—'}
                  </td>
                  <td className="px-5 py-2.5 text-text-muted">{rows ?? '—'}</td>
                  <td className="px-5 py-2.5">
                    {lastRunNew != null && lastRunNew > 0
                      ? <span className="text-neon-green">+{lastRunNew}</span>
                      : <span className="text-text-muted">—</span>}
                  </td>
                  <td className="px-5 py-2.5 text-text-muted text-xs font-mono" dir="ltr">
                    {rec?.updated_at ? rec.updated_at.slice(0, 16).replace('T', ' ') : '—'}
                  </td>
                  <td className="px-5 py-2.5">
                    {progress?.current === tf ? (
                      <span className="inline-flex items-center gap-1 text-neon-yellow text-xs">
                        <Loader2 className="h-3 w-3 animate-spin" /> جارٍ
                      </span>
                    ) : err ? (
                      <span className="inline-flex items-center gap-1 text-neon-red text-xs" title={err}>
                        <AlertTriangle className="h-3 w-3" /> خطأ: {err.slice(0, 40)}
                      </span>
                    ) : db?.rows ? (
                      <span className="inline-flex items-center gap-1 text-neon-green text-xs">
                        <CheckCircle2 className="h-3 w-3" /> مخزّن ({db.rows.toLocaleString()})
                      </span>
                    ) : rec?.last_ts ? (
                      <span className="inline-flex items-center gap-1 text-neon-green text-xs">
                        <CheckCircle2 className="h-3 w-3" /> محدّث
                      </span>
                    ) : (
                      <span className="text-text-muted text-xs">لم يُستورد</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-text-muted leading-relaxed">
        عند تشغيل المشروع يتم التحديث تلقائيًا من آخر نقطة توقف في السجل حتى لحظة التشغيل،
        وعند الإغلاق يُحفظ نقطة توقف نهائية في سجل البيانات.
        سجل الـ features التدريبي يُبنى من شموع M5 الحية (نفس مخطط التشغيل الفعلي) —
        لا يُدمَج فريم H1 في سجل التدريب بعد توحيد الفريم.
      </p>
    </div>
  );
}
