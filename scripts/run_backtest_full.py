"""B-01 — full-history chunked backtest with checkpointing (resumable).

Runs the event-driven engine over the entire M5 snapshot (14+ months) in
chunks, persisting state after every chunk so an interruption never loses
progress. Feature mode = FULL live vector (300-bar window, exact live path).

State file:  data/backtest/full_run_state.json
Report:      data/backtest/reports/b01_full.json

Low CPU priority by default (the live trading services share this machine).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import logging

logging.getLogger("services.nexus_ml.rule_based_signal").setLevel(logging.WARNING)
try:
    import psutil  # best-effort low priority
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except Exception:
    pass

from shared.logger import get_logger  # noqa: E402

logger = get_logger("backtest.full")

STATE_PATH = ROOT / "data" / "backtest" / "full_run_state.json"
REPORT_PATH = ROOT / "data" / "backtest" / "reports" / "b01_full.json"
CHUNK_BARS = 6000


def load_state() -> dict | None:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return None


def save_state(state: dict, chunk_start: int) -> None:
    state = {**state, "chunk_start": chunk_start,
             "saved_at": datetime.now(timezone.utc).isoformat()}
    STATE_PATH.write_text(json.dumps(state, indent=1))
    logger.info("checkpoint saved at bar %d (trades=%d balance=%.2f)",
                chunk_start, len(state["trades"]), state["balance"])


def main() -> None:
    from services.nexus_backtest.dataset import load_snapshot, validate_snapshot
    from services.nexus_backtest.engine import BacktestEngine

    df = load_snapshot()
    problems = validate_snapshot(df)
    if problems:
        logger.warning("snapshot problems: %s", problems)

    n = len(df)
    start0 = 400  # leave the 300-bar live window warm-up before the first signal
    state = load_state()
    chunk_start = state["chunk_start"] if state else start0
    logger.info("full run: bars=%d start_chunk=%d", n, chunk_start)

    eng = BacktestEngine()
    res_trades_len = len(state["trades"]) if state else 0

    while chunk_start < n - 1:
        chunk_end = min(chunk_start + CHUNK_BARS, n - 1)
        t0 = time.time()
        res = eng.run(
            df, start_idx=chunk_start, end_idx=chunk_end,
            lean_features=False, resume_state=state,
            close_at_end=(chunk_end >= n - 1),
        )
        state = eng.state(res, getattr(res, "_open_pos", None), res.last_entry_bar)
        save_state(state, chunk_end)
        logger.info("chunk %d..%d done in %.0fs (trades=%d balance=%.2f)",
                    chunk_start, chunk_end, time.time() - t0,
                    len(res.trades), eng.balance)
        chunk_start = chunk_end

    summary = res.summary()
    summary["snapshot_bars"] = n
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary["trades_detail"] = state["trades"]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    logger.info("FULL RUN COMPLETE: %s", json.dumps({k: v for k, v in summary.items() if k != 'trades_detail'}, indent=1))
    # clean checkpoint after completion
    STATE_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
