'use client';

import { useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import { requestHalt, useTradeControls } from '@/stores/tradeControls';
import { cn } from '@/lib/utils';

export function KillSwitch({ compact = false }: { compact?: boolean }) {
  const { snapshot, busy } = useTradeControls();
  const [action, setAction] = useState<'halt' | 'release' | null>(null);
  const [pin, setPin] = useState('');
  const halted = snapshot?.manual_halt === true;
  const close = () => { setAction(null); setPin(''); };
  return (
    <>
      <button disabled={busy} onClick={() => { setPin(''); setAction(halted ? 'release' : 'halt'); }}
        title={halted ? 'Release the operator halt (AUTO stays off)' : 'Request a halt on new entries'}
        className={cn('flex items-center gap-1.5 rounded-lg border font-bold text-neon-red disabled:opacity-60',
          compact ? 'px-3 py-1.5 text-xs' : 'px-5 py-3 text-sm', halted ? 'border-neon-red bg-neon-red/20' : 'border-neon-red/40')}>
        <ShieldAlert className="h-4 w-4" /> {halted ? 'HALTED · Release' : 'Stop entries'}
      </button>
      {action && <div className="fixed inset-0 z-[200] grid place-items-center bg-bg-overlay">
        <form role="dialog" aria-label="Operator halt" className="w-80 space-y-4 rounded-xl border border-neon-red/50 bg-bg-card p-5 text-sm"
          onSubmit={async e => {
            e.preventDefault();
            if (!pin || busy) return;
            const submitted = pin, active = action === 'halt';
            close();
            await requestHalt(active, submitted);
          }}>
          <h3 className="font-bold">{action === 'halt' ? 'Emergency stop' : 'Release manual halt'}</h3>
          <p>{action === 'halt'
            ? 'Stops new entries at the final check. Orders already past that check may still reach the broker. Open positions are not closed.'
            : 'AUTO stays off. Risk and reconciliation pauses remain in force.'}</p>
          <label className="block">Gateway PIN
            <input autoFocus required autoComplete="off" type="password" value={pin} onChange={e => setPin(e.target.value)}
              onKeyDown={e => { if (e.key === 'Escape') close(); }} className="mt-1 w-full rounded bg-bg-input px-2 py-1" />
          </label>
          <button type="button" onClick={close} className="mr-4">Cancel</button>
          <button type="submit" disabled={busy || !pin} className="font-bold text-neon-red disabled:opacity-50">
            {action === 'halt' ? 'Confirm stop' : 'Release halt'}
          </button>
        </form>
      </div>}
    </>
  );
}
