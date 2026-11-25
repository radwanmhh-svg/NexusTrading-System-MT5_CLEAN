'use client';

import { useState } from 'react';
import { Bot, BotOff } from 'lucide-react';
import { requestAuto, useTradeControls, type ExecutionMode } from '@/stores/tradeControls';
import { cn } from '@/lib/utils';

export function AutoTradeToggle({ compact = false }: { compact?: boolean }) {
  const { snapshot, busy } = useTradeControls();
  const [confirm, setConfirm] = useState<ExecutionMode | null>(null);
  // If an independent risk hold masks a previously armed AUTO, let the
  // operator disarm it; never interpret that click as an enable.
  const armed = snapshot?.auto_requested === true;
  const canChange = !!snapshot && !busy && (armed || snapshot.entry_block === 'auto_off');
  const label = busy ? 'Confirming…' : !snapshot ? 'Unknown' : snapshot.auto_trade ? 'Auto' : armed ? 'Held' : 'Off';
  return (
    <>
      <button disabled={!canChange} title={snapshot?.entry_block ?? 'Auto trading'} aria-label={`Auto trading: ${label}`}
        className={cn('flex items-center gap-1.5 rounded-lg border font-mono font-bold disabled:opacity-60',
          compact ? 'px-2.5 py-1.5 text-xs' : 'px-4 py-3 text-sm',
          snapshot?.auto_trade ? 'border-neon-green/40 text-neon-green' : 'border-border-primary text-text-secondary')}
        onClick={() => armed ? void requestAuto(false) : setConfirm(snapshot?.mode ?? null)}>
        {snapshot?.auto_trade ? <Bot className="h-3.5 w-3.5" /> : <BotOff className="h-3.5 w-3.5" />}
        {label}
      </button>
      {confirm && <div className="fixed inset-0 z-[200] grid place-items-center bg-bg-overlay">
        <div role="dialog" aria-label="Enable automatic entries" className="w-72 space-y-3 rounded-xl border border-border-primary bg-bg-card p-4 text-sm">
          <p>Allow new automatic entries in {confirm.toUpperCase()} mode? Risk checks still apply.</p>
          <button className="mr-4" onClick={() => setConfirm(null)}>Cancel</button>
          <button className="font-bold text-neon-green" disabled={!canChange || armed || snapshot?.mode !== confirm}
            onClick={() => { setConfirm(null); void requestAuto(true, confirm); }}>Enable</button>
        </div>
      </div>}
    </>
  );
}
