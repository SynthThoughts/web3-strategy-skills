#!/usr/bin/env python3
"""V3 Range Order primitives — single-sided LP positions used as zero-slippage
limit orders. Built on top of cl_lp.py's onchainos helpers and PoolConfig.

Concepts (WETH/USDC pool, token0=WETH, token1=USDC, price = USDC per WETH):
  - Mint USDC in range BELOW current price ([L, U] with U < current_tick):
      100% USDC at mint. As price drops into range, V3 swaps USDC → WETH.
      Semantics: limit BUY WETH around price U (the top of range).
  - Mint WETH in range ABOVE current price ([L, U] with L > current_tick):
      100% WETH at mint. As price rises into range, V3 swaps WETH → USDC.
      Semantics: limit SELL WETH around price L (the bottom of range).

Module is pool-agnostic: uses PoolConfig passed in or loaded from current
cl_lp CFG.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cl_lp import (
    INVESTMENT_ID, POOL_CHAIN, TICK_SPACING, TOKEN0, TOKEN1,
    NATIVE_TOKEN, USDC_ADDR, WALLET_ADDR,
    GAS_RESERVE_ETH, MIN_TRADE_USD,
    onchainos_cmd, _broadcast_defi_txs, defi_redeem, defi_claim_fees,
    get_balances, get_eth_price, get_position_detail,
    _query_all_positions, log,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_current_tick(investment_id: str = INVESTMENT_ID,
                      chain: str = POOL_CHAIN) -> int:
    """Fetch current pool tick via defi detail."""
    # Use capital_efficiency's price endpoint — most recent price point
    try:
        import capital_efficiency as ce
        prices = ce.fetch_hourly_prices(investment_id, chain)
        if prices:
            current_price = prices[-1][1]
            # Convert price to tick using PoolConfig logic
            import math
            # WETH/USDC: tick = log(price × 10^(d1-d0)) / log(1.0001)
            raw = current_price * (10 ** (int(TOKEN1["decimals"]) - int(TOKEN0["decimals"])))
            return int(math.log(raw) / math.log(1.0001))
    except Exception as e:
        log(f"get_current_tick failed: {e}")
    return 0


def snap_tick(tick: int, direction: str = "floor", spacing: int = TICK_SPACING) -> int:
    """Align tick to pool's tick_spacing grid."""
    if direction == "floor":
        return (tick // spacing) * spacing
    elif direction == "ceil":
        return ((tick + spacing - 1) // spacing) * spacing
    else:
        # Round-to-nearest spacing
        return int(round(tick / spacing)) * spacing


# ── Mint ─────────────────────────────────────────────────────────────────────

def mint_single_sided(
    side: str,                 # "sell_weth" or "buy_weth"
    amount_raw: int,           # in token's base units (wei for ETH, μunits for USDC)
    tick_lower: int,
    tick_upper: int,
    investment_id: str = INVESTMENT_ID,
    chain: str = POOL_CHAIN,
) -> tuple[bool, str]:
    """Mint a single-sided V3 position. Returns (ok, new_token_id_or_error).

    side="sell_weth": range must be ABOVE current_tick (tick_lower > current);
                     100% WETH deposit; V3 auto-swaps to USDC as price rises.
    side="buy_weth":  range must be BELOW current_tick (tick_upper < current);
                     100% USDC deposit; V3 auto-swaps to WETH as price falls.
    """
    current_tick = get_current_tick()

    # Sanity check: range on correct side of current price
    if side == "sell_weth":
        if tick_lower <= current_tick:
            return False, f"sell_weth needs tick_lower > current ({tick_lower} <= {current_tick})"
        token_addr = NATIVE_TOKEN
        decimals = int(TOKEN0["decimals"])
    elif side == "buy_weth":
        if tick_upper >= current_tick:
            return False, f"buy_weth needs tick_upper < current ({tick_upper} >= {current_tick})"
        token_addr = USDC_ADDR
        decimals = int(TOKEN1["decimals"])
    else:
        return False, f"invalid side: {side}"

    # Build user_input — onchainos accepts single-entry array for single-sided
    user_input = json.dumps([{
        "chainIndex": "8453" if chain == "base" else "1",
        "coinAmount": str(int(amount_raw)),
        "tokenAddress": token_addr,
        "tokenPrecision": str(decimals),
    }])

    log(f"RO mint ({side}): {amount_raw / (10**decimals):.6f} "
        f"{'WETH' if side=='sell_weth' else 'USDC'} → tick [{tick_lower}, {tick_upper}]")

    pre_tids = set(p.get("tokenId", "") for p in (_query_all_positions() or []))

    result = onchainos_cmd([
        "defi", "deposit",
        "--investment-id", investment_id,
        "--address", WALLET_ADDR,
        "--chain", chain,
        "--user-input", user_input,
        "--tick-lower", str(tick_lower),
        "--tick-upper", str(tick_upper),
    ], timeout=60)

    if not (result and result.get("ok")):
        return False, f"calldata failed: {json.dumps(result)[:200] if result else 'no response'}"

    if not _broadcast_defi_txs(result, "ro_mint"):
        return False, "broadcast failed"

    # Poll for new token_id (up to 90s)
    new_tid = None
    for i in range(12):
        time.sleep(8)
        post = _query_all_positions() or []
        new_ids = [p.get("tokenId", "") for p in post
                   if p.get("tokenId", "") not in pre_tids]
        if new_ids:
            new_tid = max(new_ids, key=lambda x: int(x) if x else 0)
            break

    if not new_tid:
        return True, "minted (token_id unconfirmed via API)"
    return True, new_tid


# ── Introspection ────────────────────────────────────────────────────────────

def read_position_ticks(token_id: str) -> Optional[dict]:
    """Read tick range + liquidity from NPM contract via Base RPC."""
    import subprocess
    NPM = "0x03a520b32c04bf3beef7beb72e919cf822ed34f1"
    hex_tid = format(int(token_id), "064x")
    calldata = f"0x99fbab88{hex_tid}"
    rpc = "https://mainnet.base.org"
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": NPM, "data": calldata}, "latest"],
    })
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", rpc,
             "-H", "Content-Type: application/json",
             "-d", body],
            capture_output=True, text=True, timeout=10,
        )
        raw = json.loads(r.stdout).get("result", "")
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
        }
    except Exception as e:
        log(f"read_position_ticks failed: {e}")
        return None


def fill_status(token_id: str) -> dict:
    """Compute how much of this range order has been filled.

    Semantics:
      - Range ABOVE current_tick (sell_weth):
          0% filled until price rises into range.
          100% filled when price exceeds tick_upper (all WETH → USDC).
      - Range BELOW current_tick (buy_weth):
          0% filled until price drops into range.
          100% filled when price falls below tick_lower (all USDC → WETH).
    """
    pos = read_position_ticks(token_id)
    if not pos:
        return {"error": "position not found"}
    cur = get_current_tick()
    tl, th = pos["tickLower"], pos["tickUpper"]

    # Determine side from position context (we can't tell from ticks alone if
    # we never saw current at mint time). Use a robust heuristic:
    #   range fully above current_tick → assumed sell_weth
    #   range fully below current_tick → assumed buy_weth
    #   current INSIDE → actively being filled
    side = None
    if cur < tl:
        side = "sell_weth"     # pre-activation (price below sell range)
        filled_pct = 0.0
    elif cur > th:
        side = "buy_weth"      # pre-activation (price above buy range)
        filled_pct = 0.0
    else:
        # current ∈ [tl, th]: actively being filled. For sell_weth, position
        # is filled as cur moves from tl → th. For buy_weth, reversed.
        # Without side metadata we can't disambiguate — report neutrally.
        # We compute both readings:
        sell_progress = (cur - tl) / (th - tl) * 100  # for sell_weth
        buy_progress = (th - cur) / (th - tl) * 100   # for buy_weth
        return {
            "token_id": token_id,
            "tick_lower": tl,
            "tick_upper": th,
            "current_tick": cur,
            "liquidity_npm": pos["liquidity"],
            "in_range": True,
            "sell_weth_filled_pct": round(sell_progress, 2),
            "buy_weth_filled_pct": round(buy_progress, 2),
        }
    return {
        "token_id": token_id,
        "tick_lower": tl,
        "tick_upper": th,
        "current_tick": cur,
        "liquidity_npm": pos["liquidity"],
        "in_range": False,
        "inferred_side": side,
        "filled_pct": filled_pct,
        "note": "pre-activation — price on wrong side of range",
    }


def close_range_order(token_id: str) -> bool:
    """Claim any fees + redeem (burns the NFT)."""
    try:
        defi_claim_fees(token_id)
    except Exception:
        pass
    return defi_redeem(token_id)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_mint = sub.add_parser("mint", help="Mint single-sided range order")
    p_mint.add_argument("side", choices=["sell_weth", "buy_weth"])
    p_mint.add_argument("--amount-usd", type=float, required=True,
                        help="Approximate USD value of deposit")
    p_mint.add_argument("--offset-bps", type=int, default=50,
                        help="Bps away from current price for range start (50=0.5%)")
    p_mint.add_argument("--width-ticks", type=int, default=1,
                        help="Range width in tick_spacing units (1=one spacing)")
    p_mint.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    p_status = sub.add_parser("status", help="Check RO fill status")
    p_status.add_argument("token_id")

    p_close = sub.add_parser("close")
    p_close.add_argument("token_id")

    p_current = sub.add_parser("current", help="Show current pool tick/price")

    args = ap.parse_args()

    if args.cmd == "current":
        ct = get_current_tick()
        import math
        price = (1.0001 ** ct) * (10 ** (int(TOKEN0["decimals"]) - int(TOKEN1["decimals"])))
        print(f"current_tick = {ct}  price ≈ ${price:.2f}")
        return

    if args.cmd == "mint":
        current_tick = get_current_tick()
        eth_price = get_eth_price() or 0
        spacing = TICK_SPACING
        width = args.width_ticks * spacing

        if args.side == "sell_weth":
            # Range above current: [current + offset_ticks, current + offset_ticks + width]
            offset_ticks = int(args.offset_bps / 10000 / 0.0001)  # bps → ticks
            tick_lower = snap_tick(current_tick + offset_ticks, "ceil", spacing)
            tick_upper = tick_lower + width
            amount_raw = int(args.amount_usd / eth_price * (10 ** int(TOKEN0["decimals"])))
        else:
            # Range below current: [current - offset - width, current - offset]
            offset_ticks = int(args.offset_bps / 10000 / 0.0001)
            tick_upper = snap_tick(current_tick - offset_ticks, "floor", spacing)
            tick_lower = tick_upper - width
            amount_raw = int(args.amount_usd * (10 ** int(TOKEN1["decimals"])))

        # Price preview
        import math
        p_lo = (1.0001 ** tick_lower) * (10 ** (int(TOKEN0["decimals"]) - int(TOKEN1["decimals"])))
        p_hi = (1.0001 ** tick_upper) * (10 ** (int(TOKEN0["decimals"]) - int(TOKEN1["decimals"])))
        print(f"current tick={current_tick} (${eth_price:.2f})")
        print(f"planned RO range: tick [{tick_lower}, {tick_upper}] price ${p_lo:.2f}-${p_hi:.2f}")
        print(f"amount: {amount_raw / (10 ** (int(TOKEN0['decimals'] if args.side=='sell_weth' else TOKEN1['decimals']))):.6f} "
              f"{'WETH' if args.side=='sell_weth' else 'USDC'}")

        if not args.yes:
            confirm = input("Proceed with mint? (yes/NO): ")
            if confirm.lower() != "yes":
                print("Aborted.")
                return

        ok, info = mint_single_sided(args.side, amount_raw, tick_lower, tick_upper)
        print(f"{'✓' if ok else '✗'} {info}")

    elif args.cmd == "status":
        print(json.dumps(fill_status(args.token_id), indent=2))

    elif args.cmd == "close":
        print(f"closing {args.token_id}...")
        ok = close_range_order(args.token_id)
        print("✓ closed" if ok else "✗ failed")


if __name__ == "__main__":
    _cli()
