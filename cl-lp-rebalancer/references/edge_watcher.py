#!/usr/bin/env python3
"""Edge Watcher — lightweight high-frequency edge lifecycle monitor.

Runs every minute via zeroclaw cron to close edges quickly after fill
without waiting for the 5-min main tick cycle. Complement to cl_lp.tick.

Behavior:
  - Acquire same flock as main tick (skip if tick is running)
  - If state.edges is empty → quick exit (no work)
  - For each edge: compute fill_pct; close if ≥ COMPLETE_THRESHOLD or
    age > timeout
  - Does NOT mint new main or run any other logic — leaves that to tick

Why separate from tick: main tick does lots of work (fetch ATR, risk
checks, CE scoring, snapshot building) — too expensive to run every
minute. Watcher does only the essential edge fill check.

Scheduled via zeroclaw cron:
  * * * * *   LP_AUTO_INSTANCE_DIR=... python3 edge_watcher.py
"""
from __future__ import annotations

import fcntl
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cl_lp
from cl_lp import (
    load_state, save_state, log, emit, price_to_tick,
    get_eth_price, LOCK_FILE, OOR_EDGE_TIMEOUT_HOURS,
)
from edge_manager import Edge, load_edges, save_edges, close_edge, fill_pct


CLOSE_FILL_THRESHOLD = 95.0  # match edge_manager's COMPLETE_THRESHOLD_PCT


def _safe_isoparse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def main():
    # Try to acquire lock non-blocking: if main tick is running, skip this watcher run
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Main tick holds lock — watcher silently exits
        return

    try:
        edges = load_edges()
        if not edges:
            # no edges → no work; quick exit (silent to avoid log spam)
            return

        price = get_eth_price()
        if not price or price <= 0:
            log("edge_watcher: price fetch failed, skipping")
            return
        current_tick = price_to_tick(price)

        keep: list[Edge] = []
        now = datetime.now()
        closed_count = 0
        for e in edges:
            f = fill_pct(e, current_tick)
            age_h = 0.0
            created = _safe_isoparse(e.created_at)
            if created:
                age_h = (now - created).total_seconds() / 3600

            should_close = False
            reason = ""
            if f >= CLOSE_FILL_THRESHOLD:
                should_close = True
                reason = f"fill {f:.1f}% >= {CLOSE_FILL_THRESHOLD}%"
            elif age_h > OOR_EDGE_TIMEOUT_HOURS:
                should_close = True
                reason = f"age {age_h:.1f}h > {OOR_EDGE_TIMEOUT_HOURS}h timeout"

            if should_close:
                log(f"edge_watcher: closing {e.token_id} ({e.side}) — {reason}")
                ok = close_edge(e)
                if ok:
                    closed_count += 1
                    emit("edge_closed", {
                        "token_id": e.token_id,
                        "side": e.side,
                        "fill_pct": round(f, 2),
                        "age_hours": round(age_h, 2),
                        "reason": reason,
                    }, notify=True)
                else:
                    log(f"  close failed, keeping in state")
                    keep.append(e)
            else:
                # still active — keep
                keep.append(e)

        if closed_count:
            save_edges(keep)
            log(f"edge_watcher: closed {closed_count}/{len(edges)} edge(s); "
                f"{len(keep)} remaining")
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fd.close()


if __name__ == "__main__":
    main()
