'use client';

import { Shield } from 'lucide-react';
import { useTradeControls } from '@/stores/tradeControls';

export function TradeControlNotice() {
  const { snapshot, busy, actionError, readError } = useTradeControls();
  if (snapshot && !snapshot.entry_block && !actionError) return null;
  return (
    <div role="status" aria-live="polite" className="flex flex-wrap items-center justify-center gap-2 bg-bg-secondary p-1 text-[11px] font-mono text-neon-gold">
      <Shield className="h-3 w-3" />
      {busy ? 'Confirming controls…' : !snapshot ? `Controls UNKNOWN${readError ? ': ' + readError : ''}` :
        snapshot.entry_block ? `New entries blocked: ${snapshot.entry_block}` : ''}
      {actionError && <strong role="alert" className="text-neon-red">{actionError}</strong>}
    </div>
  );
}
