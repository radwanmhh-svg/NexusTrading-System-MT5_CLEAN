'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  Map, RefreshCw, CheckCircle2, Loader2, AlertTriangle, PauseCircle,
  CircleDashed, Target, GitBranch, History as HistoryIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9000';

const STATUS_STYLE: Record<string, { icon: React.ElementType; cls: string; label: string }> = {
  COMPLETED:    { icon: CheckCircle2,   cls: 'text-neon-green', label: 'مكتملة' },
  IN_PROGRESS:  { icon: Loader2,        cls: 'text-neon-yellow', label: 'قيد التنفيذ' },
  BLOCKED:      { icon: AlertTriangle,  cls: 'text-neon-red', label: 'متعثرة' },
  NEEDS_REVIEW: { icon: AlertTriangle,  cls: 'text-neon-orange', label: 'تحتاج مراجعة' },
  FAILED:       { icon: AlertTriangle,  cls: 'text-neon-red', label: 'فشلت' },
  DEFERRED:     { icon: PauseCircle,    cls: 'text-text-muted', label: 'مؤجلة' },
  NOT_STARTED:  { icon: CircleDashed,   cls: 'text-text-muted', label: 'لم تبدأ' },
};

interface Task { id: string; title: string; status: string; evidence?: string | null; tests?: string[]; details?: string[] }
interface Phase { id: string; title: string; status: string; progress: number; tasks: Task[] }
interface Plan {
  meta?: { project?: string; symbol?: string; broker?: string; last_updated?: string };
  current?: { phase_id?: string; task_id?: string; checkpoint?: string; next_step?: string };
  phases?: Phase[];
  open_issues?: { id: string; issue: string; severity: string }[];
  decisions?: { id: string; date: string; decision: string; status: string }[];
}

function StatusChip({ status }: { status: string }) {
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.NOT_STARTED;
  const Icon = s.icon;
  return (
    <span className={cn('inline-flex items-center gap-1 text-xs font-semibold', s.cls)}>
      <Icon className={cn('h-3 w-3', status === 'IN_PROGRESS' && 'animate-spin')} /> {s.label}
    </span>
  );
}

function Bar({ value, cls }: { value: number; cls?: string }) {
  return (
    <div className="h-1.5 rounded-full bg-bg-primary overflow-hidden">
      <div className={cn('h-full rounded-full', cls || 'bg-neon-green')} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}

export default function RoadmapPage() {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/dev/plan`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPlan(await res.json());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const phases = plan?.phases ?? [];
  const completedPhases = phases.filter(p => p.status === 'COMPLETED').length;
  const allTasks = phases.flatMap(p => p.tasks);
  const doneTasks = allTasks.filter(t => t.status === 'COMPLETED').length;
  const overall = allTasks.length ? Math.round((doneTasks / allTasks.length) * 100) : 0;
  const openIssues = plan?.open_issues ?? [];
  const decisions = plan?.decisions ?? [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="h-10 w-10 rounded-lg bg-neon-green/10 border border-neon-green/30 flex items-center justify-center">
          <Map className="h-5 w-5 text-neon-green" />
        </div>
        <div>
          <h1 className="text-lg font-bold text-text-primary">خطة التطوير — Development Roadmap</h1>
          <p className="text-xs text-text-muted">
            {plan?.meta?.project ?? 'Nexus'} · {plan?.meta?.symbol ?? ''} · {plan?.meta?.broker ?? ''}
            {plan?.meta?.last_updated ? ` · آخر تحديث: ${plan.meta.last_updated.slice(0, 16).replace('T', ' ')}` : ''}
          </p>
        </div>
        <button onClick={load} className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border-primary text-xs text-text-muted hover:text-text-primary hover:border-neon-green/40">
          <RefreshCw className="h-3.5 w-3.5" /> تحديث
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-neon-red/40 bg-neon-red/5 p-4 flex items-center gap-3">
          <AlertTriangle className="h-4 w-4 text-neon-red" />
          <span className="text-sm text-neon-red">تعذر تحميل الخطة: {error} — تأكد أن الخدمة تعمل</span>
        </div>
      )}

      {/* Summary */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="rounded-xl border border-border-primary bg-bg-card p-4">
          <p className="text-xs text-text-muted">الإنجاز الكلي</p>
          <p className="text-2xl font-bold text-neon-green mt-1">{overall}%</p>
          <Bar value={overall} />
        </div>
        <div className="rounded-xl border border-border-primary bg-bg-card p-4">
          <p className="text-xs text-text-muted">مراحل مكتملة</p>
          <p className="text-2xl font-bold text-text-primary mt-1">{completedPhases}/{phases.length}</p>
        </div>
        <div className="rounded-xl border border-border-primary bg-bg-card p-4">
          <p className="text-xs text-text-muted">مهام مكتملة</p>
          <p className="text-2xl font-bold text-text-primary mt-1">{doneTasks}/{allTasks.length}</p>
        </div>
        <div className="rounded-xl border border-border-primary bg-bg-card p-4">
          <p className="text-xs text-text-muted">قيد التنفيذ الآن</p>
          <p className="text-2xl font-bold text-neon-yellow mt-1">{allTasks.filter(t => t.status === 'IN_PROGRESS').length}</p>
        </div>
      </div>

      {/* Current checkpoint */}
      {plan?.current && (
        <div className="rounded-xl border border-neon-green/30 bg-neon-green/5 p-4 flex items-start gap-3">
          <Target className="h-4 w-4 text-neon-green mt-0.5 shrink-0" />
          <div className="text-sm">
            <p className="text-neon-green font-bold">
              المرحلة الحالية: {plan.current.phase_id} · المهمة: {plan.current.task_id}
            </p>
            <p className="text-text-muted mt-1">آخر نقطة توقف: {plan.current.checkpoint}</p>
            <p className="text-text-muted">الخطوة التالية: {plan.current.next_step}</p>
          </div>
        </div>
      )}

      {/* Phases */}
      <div className="space-y-3">
        {phases.map((phase) => (
          <div key={phase.id} className="rounded-xl border border-border-primary bg-bg-card overflow-hidden">
            <div className="px-5 py-3 flex items-center gap-3 border-b border-border-primary/60">
              <span className="font-mono text-xs text-neon-green">{phase.id}</span>
              <span className="font-bold text-sm text-text-primary">{phase.title}</span>
              <span className="ml-auto"><StatusChip status={phase.status} /></span>
              <span className="text-xs text-text-muted w-10 text-left">{phase.progress}%</span>
              <div className="w-20"><Bar value={phase.progress} /></div>
            </div>
            <div className="divide-y divide-border-primary/40">
              {phase.tasks.map((task) => (
                <div key={task.id} className="px-5 py-2.5 flex items-center gap-3 text-sm">
                  <span className="font-mono text-[10px] text-text-muted w-12">{task.id}</span>
                  <span className="text-text-primary flex-1">{task.title}</span>
                  {task.evidence && (
                    <span className="hidden md:inline text-[10px] text-text-muted truncate max-w-[280px]" dir="auto">{task.evidence}</span>
                  )}
                  <span className="shrink-0"><StatusChip status={task.status} /></span>
                </div>
              ))}
              {phase.tasks.some(t => t.details?.length) && (
                <div className="px-5 py-2 border-t border-border-primary/40">
                  <details>
                    <summary className="cursor-pointer text-[11px] text-neon-green select-none">تفاصيل مهام هذه المرحلة ▾</summary>
                    <div className="mt-2 space-y-3">
                      {phase.tasks.filter(t => t.details?.length).map(t => (
                        <div key={t.id} className="rounded-lg border border-border-primary/60 bg-bg-primary/40 p-3">
                          <p className="text-xs font-bold text-text-primary mb-1.5">
                            <span className="font-mono text-neon-green mr-2">{t.id}</span>{t.title}
                          </p>
                          <ol className="space-y-1 list-decimal list-inside">
                            {t.details!.map((s, i) => (
                              <li key={i} className="text-[11px] text-text-muted leading-relaxed" dir="auto">{s}</li>
                            ))}
                          </ol>
                          {t.evidence && (
                            <p className="mt-2 text-[10px] text-text-muted" dir="auto">الدليل: {t.evidence}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Issues + decisions */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="rounded-xl border border-border-primary bg-bg-card p-5">
          <h2 className="text-sm font-bold text-text-primary flex items-center gap-2 mb-3">
            <AlertTriangle className="h-4 w-4 text-neon-orange" /> المشكلات المفتوحة والمخاطر
          </h2>
          <ul className="space-y-2">
            {openIssues.map((i) => (
              <li key={i.id} className="text-xs text-text-muted flex gap-2">
                <span className={cn('font-mono shrink-0', i.severity === 'medium' ? 'text-neon-yellow' : 'text-text-muted')}>{i.id}</span>
                <span dir="auto">{i.issue}</span>
              </li>
            ))}
            {!openIssues.length && <li className="text-xs text-text-muted">لا مشكلات مفتوحة</li>}
          </ul>
        </div>
        <div className="rounded-xl border border-border-primary bg-bg-card p-5">
          <h2 className="text-sm font-bold text-text-primary flex items-center gap-2 mb-3">
            <GitBranch className="h-4 w-4 text-neon-green" /> القرارات الهندسية المعتمدة
          </h2>
          <ul className="space-y-2">
            {decisions.map((d) => (
              <li key={d.id} className="text-xs text-text-muted">
                <span className="font-mono text-neon-green">{d.id}</span> · <span dir="auto">{d.decision}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <p className="text-[11px] text-text-muted flex items-center gap-1.5">
        <HistoryIcon className="h-3 w-3" />
        المصدر: <span className="font-mono" dir="ltr">docs/progress.json</span> — يُحدَّث إلزاميًا بعد كل مهمة (دليل: evidence + اختبارات)
      </p>
    </div>
  );
}
