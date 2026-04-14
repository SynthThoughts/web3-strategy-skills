#!/usr/bin/env python3
"""Edge Manager — orchestrates single-sided V3 Range Orders ("edges") that
sit just outside the main position's bounds as zero-slippage limit orders.

Lifecycle:
    plan_edges()  → compute tick positions for sell-side + buy-side edges
    mint_edge()   → deploy via direct NPM calls (range_order_direct)
    check_fill()  → poll current_tick vs range; detect activation/completion
    close_edge()  → multicall decreaseLiquidity + collect + burn in one tx
    cascade()     → when an edge fills, close it and open the next outer one

State:
    ~/.lp-auto/instances/<name>/edges.json  (or legacy cl_lp_state["edges"])
    [
      {"token_id", "side", "tick_lower", "tick_upper",
       "amount_raw", "token", "created_at", "created_tick", "liquidity"},
      ...
    ]

Side semantics (WETH/USDC pool, token0=WETH, token1=USDC):
  "sell_weth":  deposit WETH in range ABOVE current_tick  (tick_lower > current)
                → fills with USDC as price rises (limit SELL WETH)
  "buy_weth":   deposit USDC in range BELOW current_tick  (tick_upper < current)
                → fills with WETH as price falls (limit BUY WETH)

Cascading:
  When fill_pct >= COMPLETE_THRESHOLD (default 95%), edge is "done":
    - close_edge: recover accumulated value
    - plan_next: place new edge one width further from current price in same direction
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eth_abi import encode
from eth_utils import keccak

from cl_lp import (
    INVESTMENT_ID, POOL_CHAIN, TICK_SPACING, TOKEN0, TOKEN1,
    USDC_ADDR, WALLET_ADDR, log, load_state, save_state,
)
from range_order_direct import (
    NPM, encode_mint, encode_decrease_liquidity, encode_collect,
    encode_approve, _selector, contract_call, allowance, ensure_approval,
    _eth_call, MAX_UINT256,
)


# ── Config / constants ─────────────────────────────────────────────────────

COMPLETE_THRESHOLD_PCT = 95.0   # fill_pct above this → cascade
EDGE_WIDTH_TICKS = 2            # default edge width = N × tick_spacing
EDGE_OFFSET_TICKS = 1           # gap between main upper/lower and edge start
DEFAULT_EDGE_CAPITAL_SPLIT = 0.125  # 12.5% of total capital per edge side
SLIPPAGE_BPS = 50


# ── Data model ─────────────────────────────────────────────────────────────

@dataclass
class Edge:
    token_id: str
    side: str                # "sell_weth" | "buy_weth"
    tick_lower: int
    tick_upper: int
    amount_raw: int
    token: str               # contract address of deposited token
    created_at: str
    created_tick: int
    liquidity: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        return cls(**d)


# ── Chain state reads ──────────────────────────────────────────────────────

def read_position(token_id: str) -> Optional[dict]:
    """Read tickLower/tickUpper/liquidity/tokensOwed from NPM.positions()."""
    hex_tid = format(int(token_id), "064x")
    sel = _selector("positions(uint256)")[:4]
    data = "0x" + sel.hex() + hex_tid
    raw = _eth_call(NPM, data)
    if not raw or raw == "0x":
        return None
    h = raw[2:]
    def w(i): return int(h[i*64:(i+1)*64], 16)
    tl_raw = w(5); tl = tl_raw - (1 << 256) if tl_raw > (1 << 255) else tl_raw
    th_raw = w(6); th = th_raw - (1 << 256) if th_raw > (1 << 255) else th_raw
    return {
        "tickLower": tl,
        "tickUpper": th,
        "liquidity": w(7),
        "tokensOwed0": w(10),
        "tokensOwed1": w(11),
    }


def get_current_tick() -> int:
    """Query pool.slot0 for current tick. Pool address derived from WETH/USDC factory."""
    # Base Uniswap V3 WETH/USDC 0.3% pool address (hardcoded for now)
    POOL_ADDRS = {
        "base": {
            0.003: "0x6c561B446416e1a00E8E93e221854d6eA4171372",
            0.0005: "0xd0b53D9277642d899DF5C87A3966A349A798F224",  # WETH/USDC 0.05% Base
        },
    }
    pool = POOL_ADDRS.get(POOL_CHAIN, {}).get(0.003)
    if not pool:
        # fallback: use prices endpoint
        try:
            import capital_efficiency as ce
            prices = ce.fetch_hourly_prices(INVESTMENT_ID, POOL_CHAIN)
            if prices:
                p = prices[-1][1]
                raw = p * (10 ** (int(TOKEN1["decimals"]) - int(TOKEN0["decimals"])))
                return int(math.log(raw) / math.log(1.0001))
        except Exception:
            pass
        return 0

    # slot0() selector = 0x3850c7bd
    raw = _eth_call(pool, "0x3850c7bd")
    if not raw or raw == "0x":
        return 0
    # slot0 returns (sqrtPriceX96, tick, observationIndex, ...)
    h = raw[2:]
    tick_raw = int(h[64:128], 16)
    return tick_raw - (1 << 256) if tick_raw > (1 << 255) else tick_raw


# ── Fill status ────────────────────────────────────────────────────────────

def fill_pct(edge: Edge, current_tick: int | None = None) -> float:
    """Compute fill percentage for this edge given current_tick.
    - sell_weth:  cur < tl → 0%;  tl ≤ cur ≤ tu → linear 0→100%;  cur > tu → 100%
    - buy_weth:   cur > tu → 0%;  tl ≤ cur ≤ tu → linear 100→0%;  cur < tl → 100%
    """
    cur = current_tick if current_tick is not None else get_current_tick()
    tl, tu = edge.tick_lower, edge.tick_upper
    if edge.side == "sell_weth":
        if cur <= tl: return 0.0
        if cur >= tu: return 100.0
        return (cur - tl) / (tu - tl) * 100
    elif edge.side == "buy_weth":
        if cur >= tu: return 0.0
        if cur <= tl: return 100.0
        return (tu - cur) / (tu - tl) * 100
    return 0.0


# ── Planning ───────────────────────────────────────────────────────────────

def plan_edges(
    main_tick_lower: int,
    main_tick_upper: int,
    capital_usd_per_edge: float,
    eth_price: float,
    current_tick: int | None = None,
    edge_width_ticks: int = EDGE_WIDTH_TICKS,
    edge_offset_ticks: int = EDGE_OFFSET_TICKS,
) -> list[dict]:
    """Return two edge plans (sell-side above main, buy-side below main).
    Each plan is a dict: {side, tick_lower, tick_upper, amount_raw, token}.
    """
    spacing = TICK_SPACING
    width = edge_width_ticks * spacing
    offset = edge_offset_ticks * spacing

    # Sell-side: range above main.upper
    sell_tl = main_tick_upper + offset
    sell_tu = sell_tl + width
    sell_amount_wei = int(capital_usd_per_edge / eth_price * (10 ** int(TOKEN0["decimals"])))
    sell_plan = {
        "side": "sell_weth",
        "tick_lower": sell_tl, "tick_upper": sell_tu,
        "amount_raw": sell_amount_wei,
        "token": TOKEN0["address"],
        "capital_usd": capital_usd_per_edge,
    }

    # Buy-side: range below main.lower
    buy_tu = main_tick_lower - offset
    buy_tl = buy_tu - width
    buy_amount = int(capital_usd_per_edge * (10 ** int(TOKEN1["decimals"])))
    buy_plan = {
        "side": "buy_weth",
        "tick_lower": buy_tl, "tick_upper": buy_tu,
        "amount_raw": buy_amount,
        "token": TOKEN1["address"],
        "capital_usd": capital_usd_per_edge,
    }

    return [sell_plan, buy_plan]


# ── Minting ────────────────────────────────────────────────────────────────

def mint_edge(plan: dict, slippage_bps: int = SLIPPAGE_BPS) -> Optional[Edge]:
    """Execute a single edge mint. Returns populated Edge or None on failure.

    Pre-conditions:
      - For sell_weth: wallet has WETH (not native ETH); WETH approved to NPM
      - For buy_weth: wallet has USDC; USDC approved to NPM
    """
    side = plan["side"]
    tl, tu = plan["tick_lower"], plan["tick_upper"]
    amt = plan["amount_raw"]
    token = plan["token"]

    # Ensure approval (one-time unlimited)
    if not ensure_approval(token, amt):
        log(f"edge mint failed: approval for {token}")
        return None

    # Build mint params
    if side == "sell_weth":
        amt0_des, amt1_des = amt, 0
    else:
        amt0_des, amt1_des = 0, amt
    bps = slippage_bps / 10000
    amt0_min = int(amt0_des * (1 - bps)) if amt0_des else 0
    amt1_min = int(amt1_des * (1 - bps)) if amt1_des else 0

    deadline = int(time.time()) + 600
    calldata = encode_mint(
        token0=TOKEN0["address"], token1=TOKEN1["address"], fee=3000,
        tick_lower=tl, tick_upper=tu,
        amount0_desired=amt0_des, amount1_desired=amt1_des,
        amount0_min=amt0_min, amount1_min=amt1_min,
        recipient=WALLET_ADDR, deadline=deadline,
    )
    log(f"edge mint {side}: range [{tl}, {tu}] amt={amt}")
    ok, info = contract_call(NPM, calldata, gas_limit=800_000)
    if not ok:
        log(f"edge mint tx failed: {info}")
        return None

    # Find new token_id by scanning wallet NFTs
    # Simplest: poll NPM.balanceOf(wallet) and iterate tokenOfOwnerByIndex
    # OR parse tx receipt. For now use NPM event logs.
    tx_hash = info if info.startswith("0x") else None
    new_tid = _extract_new_token_id(tx_hash) if tx_hash else None
    if not new_tid:
        log(f"⚠ edge minted but token_id not resolvable (tx={tx_hash})")
        return None

    pos = read_position(str(new_tid)) or {}
    current_tick = get_current_tick()
    return Edge(
        token_id=str(new_tid),
        side=side,
        tick_lower=tl, tick_upper=tu,
        amount_raw=amt, token=token,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        created_tick=current_tick,
        liquidity=pos.get("liquidity", 0),
    )


def _extract_new_token_id(tx_hash: str) -> Optional[int]:
    """Parse tx receipt logs for NPM Transfer event from 0x0 → wallet."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    })
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://base.publicnode.com",
             "-H", "Content-Type: application/json", "-d", body],
            capture_output=True, text=True, timeout=10,
        )
        rc = json.loads(r.stdout).get("result")
        if not rc:
            # retry once after short wait
            time.sleep(3)
            r = subprocess.run(
                ["curl", "-s", "-X", "POST", "https://base.publicnode.com",
                 "-H", "Content-Type: application/json", "-d", body],
                capture_output=True, text=True, timeout=10,
            )
            rc = json.loads(r.stdout).get("result")
        if not rc:
            return None
        for lg in rc.get("logs", []):
            if lg["address"].lower() != NPM.lower():
                continue
            topics = lg.get("topics", [])
            if len(topics) < 4:
                continue
            if topics[0] != "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef":
                continue
            frm = "0x" + topics[1][-40:]
            if frm != "0x0000000000000000000000000000000000000000":
                continue
            return int(topics[3], 16)
    except Exception as e:
        log(f"_extract_new_token_id failed: {e}")
    return None


# ── Closing (multicall: decrease + collect) ────────────────────────────────

def encode_multicall(calldatas: list[str]) -> str:
    """Wrap multiple calls into a single NPM.multicall tx."""
    sel = _selector("multicall(bytes[])")
    # encode as dynamic array of bytes
    data_bytes = [bytes.fromhex(c[2:] if c.startswith("0x") else c) for c in calldatas]
    encoded = encode(["bytes[]"], [data_bytes])
    return "0x" + (sel + encoded).hex()


def close_edge(edge: Edge) -> bool:
    """Atomically close an edge: decreaseLiquidity(all) + collect(all) via multicall."""
    pos = read_position(edge.token_id)
    if not pos:
        log(f"close_edge: position {edge.token_id} not readable")
        return False

    deadline = int(time.time()) + 600
    liq_to_remove = pos["liquidity"]
    if liq_to_remove == 0 and pos["tokensOwed0"] == 0 and pos["tokensOwed1"] == 0:
        log(f"close_edge: edge {edge.token_id} already empty")
        return True

    parts = []
    if liq_to_remove > 0:
        parts.append(encode_decrease_liquidity(
            int(edge.token_id), liq_to_remove, 0, 0, deadline))
    # Collect sweeps both the just-decreased amounts AND any accumulated fees
    parts.append(encode_collect(int(edge.token_id), WALLET_ADDR))

    call = encode_multicall(parts)
    log(f"close_edge {edge.token_id}: multicall(decrease+collect)")
    ok, info = contract_call(NPM, call, gas_limit=600_000)
    if not ok:
        log(f"close_edge tx failed: {info}")
    return ok


# ── State persistence ──────────────────────────────────────────────────────

def load_edges() -> list[Edge]:
    state = load_state()
    return [Edge.from_dict(e) for e in state.get("edges", [])]


def save_edges(edges: list[Edge]):
    """Persist edges to state.edges, coordinating with cl_lp's flock.

    Without coordination, a concurrent cl_lp tick (scheduler-triggered)
    can read state, modify something, and write back — overwriting this
    save. Verified race 2026-04-15: edge 4969910 registered in state.edges
    was lost when a cron tick wrote back state ~60s later, then cleanup
    on next tick didn't recognize it and redeemed it as an "orphan".

    Behavior:
      - If called inside a running tick (lock already held by this
        process), write directly — parent caller guards the lock.
      - If called externally (edge_manager CLI), acquire blocking
        LOCK_EX and wait for any active tick to finish before writing.
    """
    import fcntl
    import cl_lp
    # Re-entrant: inside tick already holds the lock
    if getattr(cl_lp, "_lock_fd", None) is not None:
        state = load_state()
        state["edges"] = [e.to_dict() for e in edges]
        save_state(state)
        return
    # External call: block on lock
    lock_fd = open(cl_lp.LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = load_state()
        state["edges"] = [e.to_dict() for e in edges]
        save_state(state)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fd.close()


# ── Cascading ──────────────────────────────────────────────────────────────

def check_and_cascade(
    main_tick_lower: int,
    main_tick_upper: int,
    capital_usd_per_edge: float,
    eth_price: float,
    dry_run: bool = False,
    sides: list[str] | None = None,
) -> dict:
    """Main orchestration: check all edges for fills, close completed,
    open replacements. Returns summary dict."""
    current_tick = get_current_tick()
    edges = load_edges()
    summary = {
        "current_tick": current_tick,
        "checked": len(edges),
        "filled": [],
        "active": [],
        "closed": [],
        "minted": [],
        "errors": [],
    }

    # Step 1: evaluate each existing edge
    to_close = []
    remaining = []
    for e in edges:
        f = fill_pct(e, current_tick)
        entry = {"token_id": e.token_id, "side": e.side, "fill_pct": round(f, 1)}
        if f >= COMPLETE_THRESHOLD_PCT:
            summary["filled"].append(entry)
            to_close.append(e)
        else:
            summary["active"].append(entry)
            remaining.append(e)

    if dry_run:
        summary["dry_run"] = True
        return summary

    # Step 2: close filled edges
    for e in to_close:
        if close_edge(e):
            summary["closed"].append(e.token_id)
        else:
            summary["errors"].append(f"close {e.token_id}")
            remaining.append(e)  # keep it in state if close failed

    # Step 3: plan replacements for closed edges (if any) + fill missing sides
    sides_active = {e.side for e in remaining}
    enabled_sides = set(sides) if sides else {"sell_weth", "buy_weth"}
    plans = plan_edges(main_tick_lower, main_tick_upper,
                       capital_usd_per_edge, eth_price, current_tick)
    for plan in plans:
        if plan["side"] in sides_active:
            continue  # already have this side
        if plan["side"] not in enabled_sides:
            continue  # operator disabled this side
        edge = mint_edge(plan)
        if edge:
            remaining.append(edge)
            summary["minted"].append(edge.token_id)
        else:
            summary["errors"].append(f"mint {plan['side']}")

    save_edges(remaining)
    return summary


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="Compute edge positions (no on-chain action)")
    p_plan.add_argument("--main-lo", type=int, required=True)
    p_plan.add_argument("--main-hi", type=int, required=True)
    p_plan.add_argument("--capital", type=float, default=20.0,
                        help="USD per edge side")

    p_check = sub.add_parser("check", help="Dry-run fill status for all edges in state")
    p_check.add_argument("--main-lo", type=int)
    p_check.add_argument("--main-hi", type=int)
    p_check.add_argument("--capital", type=float, default=20.0)

    p_run = sub.add_parser("run", help="Execute cascade (close filled + mint missing)")
    p_run.add_argument("--main-lo", type=int, required=True)
    p_run.add_argument("--main-hi", type=int, required=True)
    p_run.add_argument("--capital", type=float, default=20.0)
    p_run.add_argument("--sides", default="sell_weth,buy_weth",
                       help="comma-separated sides to enable")

    p_list = sub.add_parser("list")
    p_close = sub.add_parser("close")
    p_close.add_argument("token_id")

    args = ap.parse_args()

    if args.cmd == "plan":
        import capital_efficiency as ce
        prices = ce.fetch_hourly_prices(INVESTMENT_ID, POOL_CHAIN)
        eth_price = prices[-1][1] if prices else 0
        plans = plan_edges(args.main_lo, args.main_hi, args.capital, eth_price)
        for p in plans:
            p_lo = (1.0001 ** p["tick_lower"]) * 10**(int(TOKEN0["decimals"]) - int(TOKEN1["decimals"]))
            p_hi = (1.0001 ** p["tick_upper"]) * 10**(int(TOKEN0["decimals"]) - int(TOKEN1["decimals"]))
            print(f"  {p['side']:<10} tick [{p['tick_lower']},{p['tick_upper']}] "
                  f"price ${p_lo:.0f}-${p_hi:.0f}  amount={p['amount_raw']}  "
                  f"(~${p['capital_usd']:.0f})")

    elif args.cmd == "check":
        if args.main_lo and args.main_hi:
            import capital_efficiency as ce
            prices = ce.fetch_hourly_prices(INVESTMENT_ID, POOL_CHAIN)
            eth_price = prices[-1][1] if prices else 0
            summary = check_and_cascade(args.main_lo, args.main_hi, args.capital,
                                         eth_price, dry_run=True)
        else:
            current = get_current_tick()
            edges = load_edges()
            summary = {
                "current_tick": current,
                "edges": [{"token_id": e.token_id, "side": e.side,
                           "fill_pct": round(fill_pct(e, current), 1)}
                          for e in edges],
            }
        print(json.dumps(summary, indent=2))

    elif args.cmd == "run":
        import capital_efficiency as ce
        prices = ce.fetch_hourly_prices(INVESTMENT_ID, POOL_CHAIN)
        eth_price = prices[-1][1] if prices else 0
        sides = [s.strip() for s in args.sides.split(",") if s.strip()]
        summary = check_and_cascade(args.main_lo, args.main_hi, args.capital,
                                     eth_price, sides=sides)
        print(json.dumps(summary, indent=2))

    elif args.cmd == "list":
        edges = load_edges()
        current = get_current_tick()
        for e in edges:
            f = fill_pct(e, current)
            print(f"  {e.token_id:>10}  {e.side:<10}  tick[{e.tick_lower},{e.tick_upper}]  "
                  f"fill={f:.1f}%  created@{e.created_at}")
        if not edges:
            print("  (no edges in state)")

    elif args.cmd == "close":
        edges = load_edges()
        found = next((e for e in edges if e.token_id == args.token_id), None)
        if not found:
            # Build ad-hoc Edge from NPM read
            pos = read_position(args.token_id)
            if not pos:
                print(f"✗ position {args.token_id} not found"); return
            found = Edge(
                token_id=args.token_id, side="unknown",
                tick_lower=pos["tickLower"], tick_upper=pos["tickUpper"],
                amount_raw=0, token="",
                created_at="", created_tick=0,
                liquidity=pos["liquidity"],
            )
        ok = close_edge(found)
        if ok:
            edges = [e for e in edges if e.token_id != args.token_id]
            save_edges(edges)
        print("✓ closed" if ok else "✗ failed")


if __name__ == "__main__":
    _cli()
