"""
Execution journal — one reliable record per broker action.

Reconciles MT5 ``history_deals_get``/``history_orders_get`` into the DuckDB
``execution_journal`` table, keeping Order / Deal / Position as SEPARATE
entities (they are not interchangeable):

- journal_orders : every order request, retcode, requested vs filled price
- journal_deals  : every deal (entry/exit), price, commission, swap, profit
- journal_slippage: per-deal slippage = deal price − intended price

Enables post-trade attribution: every row links back to position_id and
(optional) cycle_id so model decisions can be audited against real fills.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1


def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _ms(t: dict[str, Any], key_msc: str, key_s: str) -> int:
    """Millisecond timestamp with fallback: some MT5 workers expose `time`
    in whole seconds instead of `time_msc` (worker _asdict variance)."""
    ms = int(t.get(key_msc, 0) or 0)
    if not ms:
        sec = float(t.get(key_s, 0) or 0)
        ms = int(sec * 1000)
    return ms


def fill_missing_fields(
    existing: dict[str, Any] | None,
    mt5: dict[str, Any] | None,
) -> dict[str, Any]:
    """D-02: columns to UPDATE on an existing trades row from MT5 deal data.

    Hard rule: NEVER returns a key whose existing value is populated —
    only fills EMPTY fields (NULL / 0 for numerics, '' for strings), and
    skips no-op writes (same value). P&L / price / strategy fields are
    never touched: stored history is not rewritten (rule #1).
    """
    if not mt5:
        return {}
    FILLABLE = ("volume", "commission", "swap", "deal_ticket",
                "position_id", "magic", "comment")
    out: dict[str, Any] = {}
    for col in FILLABLE:
        cur = (existing or {}).get(col)
        new = mt5.get(col)
        if new is None:
            continue
        if isinstance(new, (int, float)):
            empty = cur is None or (isinstance(cur, (int, float)) and float(cur) == 0.0)
            if empty and float(new) != 0.0:
                out[col] = new
        else:  # string field (comment)
            empty = cur is None or str(cur).strip() == ""
            if empty and str(new).strip():
                out[col] = str(new)
    return out


def positions_to_trades(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """D-01 builder: group MT5 deals by position_id into closed trades.

    Pure function (no IO) so it is unit-testable. Rules:
    - A position is a closed trade only when it has an OUT deal.
    - net pnl = Σ(profit + commission + swap) — the broker's real numbers.
    - exit_price is volume-weighted across partial closes.
    - trade id = str(position_id) — INSERT-only, never rewrites history.
    """
    from shared.asset_specs import price_to_pips
    from shared.nexus_trades import strategy_from_comment
    from shared.sessions import detect_session

    by_pos: dict[int, list[dict[str, Any]]] = {}
    for d in deals:
        pid = int(d.get("position_id", 0) or 0)
        if not pid:
            continue
        by_pos.setdefault(pid, []).append(d)

    out: list[dict[str, Any]] = []
    for pid, ds in by_pos.items():
        ds.sort(key=lambda x: str(x.get("time", "")))
        in_deals = [d for d in ds if str(d.get("entry", "")) == "IN"]
        out_deals = [d for d in ds if str(d.get("entry", "")) == "OUT"]
        if not out_deals:
            continue  # still open — not a closed trade

        first = in_deals[0] if in_deals else ds[0]
        last_out = out_deals[-1]
        symbol = str(first.get("symbol", "") or "").upper()
        if not symbol:
            continue
        side = str(first.get("side", "") or "").upper()
        if side not in ("BUY", "SELL"):
            continue

        entry_price = float(first.get("price", 0) or 0)
        vol_out = sum(float(d.get("volume", 0) or 0) for d in out_deals)
        if vol_out > 0:
            exit_price = sum(
                float(d.get("price", 0) or 0) * float(d.get("volume", 0) or 0)
                for d in out_deals
            ) / vol_out
        else:
            exit_price = float(last_out.get("price", 0) or 0)

        pnl_usd = sum(
            float(d.get("profit", 0) or 0)
            + float(d.get("commission", 0) or 0)
            + float(d.get("swap", 0) or 0)
            for d in ds
        )
        commission = sum(float(d.get("commission", 0) or 0) for d in ds)
        swap = sum(float(d.get("swap", 0) or 0) for d in ds)

        diff = exit_price - entry_price
        signed_diff = diff if side == "BUY" else -diff
        try:
            pnl_pips = price_to_pips(symbol, signed_diff)
        except Exception:
            pnl_pips = 0.0

        opened_at = first.get("time")
        closed_at = last_out.get("time")
        if not isinstance(opened_at, datetime):
            opened_at = datetime.now(timezone.utc)
        if not isinstance(closed_at, datetime):
            closed_at = datetime.now(timezone.utc)
        duration_s = max(0.0, (closed_at - opened_at).total_seconds())

        comment = str(first.get("comment", "") or "")
        out.append({
            "id": str(pid),
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pips": round(pnl_pips, 1),
            "pnl_usd": round(pnl_usd, 2),
            "duration_s": duration_s,
            "session": detect_session(closed_at.hour).value,
            "strategy": strategy_from_comment(comment),
            "volume": vol_out,
            "commission": round(commission, 2),
            "swap": round(swap, 2),
            "deal_ticket": int(last_out.get("deal_ticket", 0) or 0),
            "position_id": pid,
            "magic": int(last_out.get("magic", 0) or 0) or None,
            "comment": comment,
            "opened_at": opened_at,
            "closed_at": closed_at,
        })
    return out


def reconcile_history(
    orders: list[dict[str, Any]],
    deals: list[dict[str, Any]],
    *,
    symbols: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Turn raw MT5 history rows into normalized journal records.

    Parameters are plain dicts (what ``mt5.history_orders_get`` /
    ``history_deals_get`` expose via ``_asdict()``).
    """
    sym_f = (symbols or set()).__contains__

    journal_orders: list[dict[str, Any]] = []
    for o in orders:
        sym = str(o.get("symbol", "") or "").upper()
        if symbols and sym not in symbols:
            continue
        journal_orders.append({
            "order_ticket": int(o.get("ticket", 0)),
            "position_id": int(o.get("position_id", 0) or 0),
            "symbol": sym,
            "side": "BUY" if int(o.get("type", 0)) == 0 else "SELL",
            "order_type": int(o.get("type", 0)),
            "volume_requested": float(o.get("volume_initial", 0) or 0),
            "volume_filled": float(o.get("volume_current", 0) or 0),
            "price_requested": float(o.get("price_open", 0) or 0),
            "state": int(o.get("state", 0)),
            "retcode": int(o.get("ret_code", 0) or 0) or int(o.get("state", 0)),
            "comment": str(o.get("comment", "") or ""),
            "time_setup": _ts(_ms(o, "time_setup_msc", "time_setup")),
            "time_done": _ts(_ms(o, "time_done_msc", "time_done")),
        })

    journal_deals: list[dict[str, Any]] = []
    journal_slippage: list[dict[str, Any]] = []
    intended_by_position: dict[int, float] = {}
    for o in orders:
        pid = int(o.get("position_id", 0) or 0)
        price = float(o.get("price_open", 0) or 0)
        if pid and price:
            intended_by_position.setdefault(pid, price)

    for t in deals:
        sym = str(t.get("symbol", "") or "").upper()
        if symbols and sym not in symbols:
            continue
        entry_type = int(t.get("entry", 0))
        row = {
            "deal_ticket": int(t.get("ticket", 0)),
            "order_ticket": int(t.get("order", 0) or 0),
            "position_id": int(t.get("position_id", 0) or 0),
            "symbol": sym,
            "side": "BUY" if int(t.get("type", 0)) == 0 else "SELL",
            "entry": "IN" if entry_type == DEAL_ENTRY_IN else "OUT",
            "volume": float(t.get("volume", 0) or 0),
            "price": float(t.get("price", 0) or 0),
            "commission": float(t.get("commission", 0) or 0),
            "swap": float(t.get("swap", 0) or 0),
            "profit": float(t.get("profit", 0) or 0),
            "comment": str(t.get("comment", "") or ""),
            "magic": int(t.get("magic", 0) or 0),
            "time": _ts(_ms(t, "time_msc", "time")),
        }
        journal_deals.append(row)

        pid = row["position_id"]
        intended = intended_by_position.get(pid)
        if entry_type == DEAL_ENTRY_IN and intended:
            direction = 1 if row["side"] == "BUY" else -1
            journal_slippage.append({
                "position_id": pid,
                "symbol": sym,
                "deal_ticket": row["deal_ticket"],
                "side": row["side"],
                "intended_price": intended,
                "filled_price": row["price"],
                "slippage": direction * (row["price"] - intended),
                "slippage_points": direction * (row["price"] - intended) / 0.01 if sym.startswith("XAU") or sym.startswith("BTC") else 0.0,
                "time": row["time"],
            })

    return {
        "orders": journal_orders,
        "deals": journal_deals,
        "slippage": journal_slippage,
    }
