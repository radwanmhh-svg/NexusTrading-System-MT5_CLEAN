/** Authoritative gateway snapshots only. No localStorage, optimistic dispatch or retries. */
import { create } from 'zustand';
import { apiClient } from '@/lib/api-client';

export type ExecutionMode = 'paper' | 'shadow' | 'live' | 'backtest';
export interface Snapshot {
  mode: ExecutionMode;
  auto_trade: boolean;
  auto_requested: boolean;
  manual_halt: boolean;
  risk_halt: boolean;
  reconcile_paused: string | null;
  entry_block: string | null;
}

interface ControlsState {
  snapshot: Snapshot | null;
  busy: boolean;
  readError: string;
  actionError: string;
}

export const useTradeControls = create<ControlsState>(() => ({
  snapshot: null, busy: false, readError: '', actionError: '',
}));

function parse(value: unknown): Snapshot {
  const s = value as Partial<Snapshot> | null;
  if (!s || !['paper', 'shadow', 'backtest', 'live'].includes(String(s.mode)) ||
      typeof s.auto_trade !== 'boolean' || typeof s.auto_requested !== 'boolean' ||
      typeof s.manual_halt !== 'boolean' || typeof s.risk_halt !== 'boolean' ||
      !(s.reconcile_paused === null || typeof s.reconcile_paused === 'string') ||
      !(s.entry_block === null || typeof s.entry_block === 'string') ||
      s.auto_trade !== (s.entry_block === null) ||
      (s.auto_trade && (!s.auto_requested || s.manual_halt || s.risk_halt || s.reconcile_paused !== null ||
                       (s.mode !== 'paper' && s.mode !== 'live')))) {
    throw new Error('Invalid gateway control snapshot');
  }
  return s as Snapshot;
}

let revision = 0; // a poll started before a write cannot overwrite its result
const message = (e: unknown) => e instanceof Error ? e.message : 'Gateway unavailable';

export async function refreshControls() {
  if (useTradeControls.getState().busy) return;
  const seq = ++revision;
  try {
    const snapshot = parse((await apiClient.systemGetMode()).data);
    if (seq === revision) useTradeControls.setState({ snapshot, readError: '' });
  } catch (e) {
    if (seq === revision) useTradeControls.setState({ snapshot: null, readError: message(e) });
  }
}

async function change(send: () => Promise<unknown>, expected: (s: Snapshot) => boolean) {
  if (useTradeControls.getState().busy) return false;
  ++revision;
  useTradeControls.setState({ busy: true, snapshot: null, actionError: '', readError: '' });
  try {
    await send();  // HTTP success alone is not confirmation
    const snapshot = parse((await apiClient.systemGetMode()).data);
    const ok = expected(snapshot);
    useTradeControls.setState({ snapshot, actionError: ok ? '' : 'Requested state not confirmed by gateway.' });
    return ok;
  } catch (e) {
    useTradeControls.setState({ snapshot: null, actionError: `Action not confirmed: ${message(e)}` });
    return false;
  } finally {
    useTradeControls.setState({ busy: false });
  }
}

export function requestAuto(enabled: boolean, expectedMode?: ExecutionMode) {
  return change(() => apiClient.systemToggleAutoTrade(enabled, expectedMode), s => s.auto_requested === enabled && s.auto_trade === enabled && (!enabled || s.mode === expectedMode));
}
export function requestHalt(active: boolean, pin: string) {
  return change(() => active ? apiClient.systemKillSwitch(pin) : apiClient.systemReleaseHalt(pin),
    s => s.manual_halt === active && !s.auto_requested && !s.auto_trade);
}
export function requestMode(mode: ExecutionMode, pin?: string) {
  return change(() => apiClient.systemSwitchMode(mode, pin), s => s.mode === mode && !s.auto_requested && !s.auto_trade);
}
