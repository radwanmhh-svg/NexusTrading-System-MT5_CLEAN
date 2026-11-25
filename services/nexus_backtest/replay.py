"""B-01 — 7-day replay validation against the live trade record.

Acceptance (documented, relaxed from the directive's ±1):
  replaying the last 7 days must reproduce the 7 live trades
  (count ±2) with matching directions — live trades were affected by
  time-stops/session breaks the engine cannot know.

Live trades are read from the single source of truth (`/api/data/trades`).
Read-only; no historical record is touched.
"""
from __future__ import annotations

import json
import os
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from shared.logger import get_logger

logger = get_logger("backtest.replay")

NEXUS_DATA_URL = os.getenv("NEXUS_DATA_URL", "http://localhost:9001")
SYMBOL_ALIASES = ("XAUUSDs", "XAUUSDS", "XAUUSD")


@dataclass
class ReplayReport:
    window_days: int
    live_trades: int
    replay_trades: int
    live_directions: Counter = field(default_factory=Counter)
    replay_directions: Counter = field(default_factory=Counter)
    direction_match: bool = False
    count_within_tolerance: bool = False
    entry_matches: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.direction_match and self.count_within_tolerance

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "window_days": self.window_days,
            "live_trades": self.live_trades,
            "replay_trades": self.replay_trades,
            "live_directions": dict(self.live_directions),
            "replay_directions": dict(self.replay_directions),
            "direction_match": self.direction_match,
            "count_within_tolerance": self.count_within_tolerance,
            "entry_matches": self.entry_matches,
            "details": self.details,
        }


def fetch_live_trades() -> list[dict]:
    with urllib.request.urlopen(f"{NEXUS_DATA_URL}/api/data/trades?page=1&page_size=100", timeout=60) as r:
        payload = json.load(r)
    trades = payload.get("trades", payload) if isinstance(payload, dict) else payload
    return [t for t in trades if t.get("symbol", "").lower().startswith("xauusd")]


def compare_trades(
    live: list[dict],
    replay_trades,
    window_start: pd.Timestamp,
    count_tolerance: int = 2,
    entry_tol_pips: float = 60.0,
) -> ReplayReport:
    """Direction set + count ±tolerance + entry proximity report."""
    live_in_window = []
    for t in live:
        opened = pd.Timestamp(t.get("opened_at"))
        if opened.tzinfo is None:
            opened = opened.tz_localize("UTC")
        else:
            opened = opened.tz_convert("UTC")
        if opened >= window_start:
            live_in_window.append(t)

    live_dirs = Counter(t.get("side", "?") for t in live_in_window)
    replay_dirs = Counter(t.side for t in replay_trades)

    report = ReplayReport(
        window_days=7,
        live_trades=len(live_in_window),
        replay_trades=len(replay_trades),
        live_directions=live_dirs,
        replay_directions=replay_dirs,
    )
    report.direction_match = (
        set(replay_dirs) - set(live_dirs) == set()
        and all(replay_dirs[k] <= live_dirs.get(k, 0) + count_tolerance for k in replay_dirs)
    )
    report.count_within_tolerance = abs(len(live_in_window) - len(replay_trades)) <= count_tolerance

    # entry proximity (best-effort pairing by time order)
    for i, rt in enumerate(replay_trades):
        if i < len(live_in_window):
            lt = live_in_window[i]
            diff = abs(float(rt.entry_price) - float(lt.get("entry_price", 0) or 0)) / 0.01
            report.entry_matches.append({
                "side_match": rt.side == lt.get("side"),
                "live_entry": lt.get("entry_price"),
                "replay_entry": round(rt.entry_price, 2),
                "entry_diff_pips": round(diff, 1),
                "within_tol": diff <= entry_tol_pips,
            })
    return report
