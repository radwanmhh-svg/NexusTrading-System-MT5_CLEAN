'use client';
/**
 * T9 (PLAN_MASTER) — صفحة Capabilities.
 * كل قدرة → endpoint / الحالة الثلاثية (defined ≠ loaded ≠ used_live) /
 * آخر تشغيل / مصدر / آخر خطأ.
 */
import { useEffect, useState } from 'react';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9000';

type Capability = {
  id: string;
  name: string;
  endpoint?: string;
  defined?: boolean;
  loaded?: boolean;
  used_live?: boolean;
  last_run?: string | null;
  source?: string | null;
  last_error?: string | null;
  is_synthetic?: boolean | null;
  dataset_id?: string | null;
  value?: string | null;
};

function StateBadge({ label, ok }: { label: string; ok?: boolean }) {
  const cls =
    ok === true
      ? 'bg-neon-green/15 text-neon-green'
      : ok === false
        ? 'bg-neon-red/15 text-neon-red'
        : 'bg-text-muted/15 text-text-muted';
  return (
    <span className={`rounded px-1.5 py-0.5 text-[9px] font-bold uppercase ${cls}`}>
      {label}: {ok === true ? 'yes' : ok === false ? 'no' : '—'}
    </span>
  );
}

export default function CapabilitiesPage() {
  const [caps, setCaps] = useState<Capability[]>([]);
  const [generatedAt, setGeneratedAt] = useState<string>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/api/capabilities`, { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        setCaps(d.capabilities ?? []);
        setGeneratedAt(d.generated_at ?? '');
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-sm font-bold uppercase tracking-wider text-neon-purple">
          Capabilities — defined ≠ loaded ≠ used_live
        </h1>
        {generatedAt && (
          <span className="text-[9px] font-mono text-text-muted">{generatedAt.slice(0, 19)}</span>
        )}
      </div>

      {error && (
        <div className="rounded border border-neon-red/30 bg-neon-red/10 p-2 text-[10px] font-mono text-neon-red">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        {caps.map((c) => (
          <div
            key={c.id}
            className="rounded border border-border-primary/20 bg-bg-card/60 p-3 font-mono"
          >
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-[11px] font-bold text-text-primary">{c.name}</span>
              <span className="text-[9px] text-text-muted">{c.id}</span>
            </div>
            {c.endpoint && (
              <div className="mb-1.5 truncate text-[9px] text-neon-cyan/80">{c.endpoint}</div>
            )}
            <div className="mb-1.5 flex flex-wrap gap-1">
              <StateBadge label="defined" ok={c.defined} />
              <StateBadge label="loaded" ok={c.loaded} />
              <StateBadge label="used_live" ok={c.used_live} />
              {c.is_synthetic === true && (
                <span className="rounded bg-neon-orange/15 px-1.5 py-0.5 text-[9px] font-bold text-neon-orange">
                  synthetic
                </span>
              )}
            </div>
            <div className="space-y-0.5 text-[9px] text-text-muted">
              {c.source && <div>source: {c.source}</div>}
              {c.last_run && <div>last_run: {String(c.last_run).slice(0, 19)}</div>}
              {c.last_error && <div className="text-neon-red">last_error: {c.last_error}</div>}
              {c.value && <div>value: {c.value}</div>}
              {c.dataset_id && <div>dataset: {c.dataset_id}</div>}
            </div>
          </div>
        ))}
      </div>

      {caps.length === 0 && !error && (
        <div className="text-[10px] font-mono text-text-muted">loading…</div>
      )}
    </div>
  );
}
