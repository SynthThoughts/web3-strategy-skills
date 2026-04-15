#!/usr/bin/env python3
"""Edge Observer — poll an active edge's live asset composition via onchainos
`defi position-detail`, print the WETH↔USDC ratio and fill%.

All reads go through onchainos (no direct RPC / NPM eth_call):
  - composition: cl_lp.get_position_detail (wraps `onchainos defi position-detail`)
  - current price: cl_lp.get_eth_price (onchainos market endpoint)
  - tick range: read from state.edges (persisted by edge_manager at mint)

Usage:
  edge_observer.py <token_id> [--interval 60] [--duration 3600]

Output (per poll):
  ts  price  WETH_amt  USDC_amt  WETH$  USDC$  WETH%  fill%
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cl_lp import (
    get_position_detail, get_eth_price, load_state,
    TOKEN0, TOKEN1,
)


def price_to_tick(price_usd: float) -> int:
    """WETH/USDC: tick = log(price_usd × 10^(d1-d0)) / log(1.0001)."""
    raw = price_usd * (10 ** (int(TOKEN1["decimals"]) - int(TOKEN0["decimals"])))
    return int(math.log(raw) / math.log(1.0001))


def find_edge_in_state(token_id: str) -> dict | None:
    state = load_state()
    for e in state.get("edges", []) or []:
        if str(e.get("token_id")) == str(token_id):
            return e
    return None


def compute_fill(side: str, tl: int, tu: int, cur: int) -> float:
    if side == "sell_weth":
        if cur <= tl: return 0.0
        if cur >= tu: return 100.0
        return (cur - tl) / (tu - tl) * 100
    else:  # buy_weth
        if cur >= tu: return 0.0
        if cur <= tl: return 100.0
        return (tu - cur) / (tu - tl) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("token_id")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--duration", type=int, default=3600)
    args = ap.parse_args()

    edge = find_edge_in_state(args.token_id)
    if not edge:
        print(f"# ⚠ token_id {args.token_id} not in state.edges")
        print(f"# Pass tick range via command args not supported yet;")
        print(f"# add it to state.edges first or use edge_manager list")
        return 1

    tl = int(edge["tick_lower"])
    tu = int(edge["tick_upper"])
    side = edge["side"]
    print(f"# Edge {args.token_id} side={side} tick[{tl},{tu}] "
          f"(created@{edge.get('created_at','?')})")
    print(f"# {'ts':<8}  {'ETH$':>8}  {'cur_t':>7}  "
          f"{'WETH':>10}  {'USDC':>10}  {'WETH$':>8}  {'USDC$':>8}  "
          f"{'W%':>5}  {'fill':>5}")

    start = time.time()
    while time.time() - start < args.duration:
        try:
            d = get_position_detail(args.token_id)
            price = get_eth_price() or 0
            cur_tick = price_to_tick(price) if price > 0 else 0
            weth_amt = usdc_amt = weth_usd = usdc_usd = 0.0
            for a in d.get("assets", []):
                sym = a.get("tokenSymbol", "").upper()
                if sym in ("WETH", "ETH"):
                    weth_amt = float(a["coinAmount"])
                    weth_usd = float(a["currencyAmount"])
                elif sym == "USDC":
                    usdc_amt = float(a["coinAmount"])
                    usdc_usd = float(a["currencyAmount"])
            total_usd = weth_usd + usdc_usd
            weth_pct = (weth_usd / total_usd * 100) if total_usd > 0 else 0.0
            fill = compute_fill(side, tl, tu, cur_tick)
            ts = time.strftime("%H:%M:%S")
            print(f"  {ts:<8}  {price:>8.2f}  {cur_tick:>7}  "
                  f"{weth_amt:>10.6f}  {usdc_amt:>10.2f}  "
                  f"{weth_usd:>8.2f}  {usdc_usd:>8.2f}  "
                  f"{weth_pct:>4.1f}%  {fill:>4.1f}%", flush=True)
        except Exception as e:
            print(f"  # err: {e}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
