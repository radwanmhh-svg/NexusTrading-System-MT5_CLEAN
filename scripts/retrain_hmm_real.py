"""T8 — إعادة تدريب HMM على بيانات حقيقية من DuckDB (وليس synthetic).

- يقرأ OHLCV الحقيقي (M5 — 100k شمعة حقيقية من ملفات الوسيط).
- يحفظ hmm_regime.pkl مع metadata: is_synthetic=False.
- بوابة live: منع نموذج synthetic في التشغيل الحي إلا بإعداد صريح
  (NEXUS_ALLOW_SYNTHETIC_HMM=1) — يُنفذ في traders_api._get_hmm_detector.
"""
from __future__ import annotations

import io, sys as _sys
if hasattr(_sys.stdout, "buffer"): _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL_DIR = ROOT / "models_store"
DB_PATH = ROOT / "data" / "nexus.duckdb"


def load_real_ohlcv(timeframes=("M5", "M15", "H1"), min_rows=500) -> np.ndarray:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        all_rows = []
        for tf in timeframes:
            df = con.execute(
                """SELECT open, high, low, close, volume FROM ohlcv
                   WHERE symbol='XAUUSD' AND timeframe=? ORDER BY timestamp""",
                [tf],
            ).df()
            if len(df) >= min_rows:
                all_rows.append(df[["open", "high", "low", "close", "volume"]].to_numpy(dtype=np.float64))
                break  # أعلى تزامن متاح أولاً
    finally:
        con.close()
    if not all_rows:
        raise SystemExit("لا بيانات حقيقية كافية لتدريب HMM")
    return np.vstack(all_rows)


def main() -> None:
    from services.nexus_ml.models.hmm_regime import HMMRegimeDetector

    ohlcv = load_real_ohlcv()
    print(f"real bars: {len(ohlcv)}")

    det = HMMRegimeDetector()
    info = det.fit(ohlcv)
    print("fit:", json.dumps({k: (round(v, 2) if isinstance(v, float) else v)
                              for k, v in info.items() if k != "state_mapping"}))
    print("state mapping:", json.dumps(info["state_mapping"]))

    hmm_path = MODEL_DIR / "hmm_regime.pkl"
    det.save(hmm_path)

    # metadata منفصلة (الـpickle لا يحملها): is_synthetic=False لأن المصدر بيانات حقيقية
    meta = {
        "model_source": "nexus",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_samples": int(len(ohlcv)),
        "data_source": "duckdb_real_ohlcv",
        "is_synthetic": False,
        "n_observations": info.get("n_observations"),
        "log_likelihood": info.get("log_likelihood"),
        "state_mapping": info.get("state_mapping"),
        "converged": info.get("converged"),
    }
    (MODEL_DIR / "hmm_metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("saved:", hmm_path, "+ hmm_metadata.json (is_synthetic=False)")


if __name__ == "__main__":
    main()
