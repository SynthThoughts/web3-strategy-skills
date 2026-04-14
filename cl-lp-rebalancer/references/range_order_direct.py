#!/usr/bin/env python3
"""Direct V3 NonfungiblePositionManager mint — bypasses OKX router which
auto-unwinds single-sided deposits.

Calls NPM.mint(MintParams) directly via onchainos `wallet contract-call`,
signed by onchainos's internal key.

Dependencies:
  - eth_abi (encode struct params)
  - eth_utils (keccak for selectors)
  - onchainos wallet contract-call
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eth_abi import encode
from eth_utils import keccak

from cl_lp import (
    INVESTMENT_ID, POOL_CHAIN, CHAIN_ID, TICK_SPACING, TOKEN0, TOKEN1,
    USDC_ADDR, WALLET_ADDR, GAS_RESERVE_ETH, log,
)

# Uniswap V3 NonfungiblePositionManager on Base
NPM_BY_CHAIN = {
    "base": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1",
    "ethereum": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    "arbitrum": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    "optimism": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    "polygon":  "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
}
NPM = NPM_BY_CHAIN.get(POOL_CHAIN, NPM_BY_CHAIN["base"])


# ── Encoding helpers ────────────────────────────────────────────────────────

def _selector(sig: str) -> bytes:
    return keccak(sig.encode())[:4]


def encode_approve(spender: str, amount: int) -> str:
    sel = _selector("approve(address,uint256)")
    data = encode(["address", "uint256"], [spender, amount])
    return "0x" + (sel + data).hex()


def encode_mint(
    token0: str, token1: str, fee: int,
    tick_lower: int, tick_upper: int,
    amount0_desired: int, amount1_desired: int,
    amount0_min: int, amount1_min: int,
    recipient: str, deadline: int,
) -> str:
    sig = "mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256))"
    sel = _selector(sig)
    data = encode(
        ["(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"],
        [(
            token0, token1, fee,
            tick_lower, tick_upper,
            amount0_desired, amount1_desired,
            amount0_min, amount1_min,
            recipient, deadline,
        )],
    )
    return "0x" + (sel + data).hex()


def encode_increase_liquidity(
    token_id: int,
    amount0_desired: int, amount1_desired: int,
    amount0_min: int, amount1_min: int,
    deadline: int,
) -> str:
    sig = "increaseLiquidity((uint256,uint256,uint256,uint256,uint256,uint256))"
    sel = _selector(sig)
    data = encode(
        ["(uint256,uint256,uint256,uint256,uint256,uint256)"],
        [(token_id, amount0_desired, amount1_desired, amount0_min, amount1_min, deadline)],
    )
    return "0x" + (sel + data).hex()


def encode_decrease_liquidity(token_id: int, liquidity: int,
                                amount0_min: int, amount1_min: int,
                                deadline: int) -> str:
    sig = "decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))"
    sel = _selector(sig)
    data = encode(
        ["(uint256,uint128,uint256,uint256,uint256)"],
        [(token_id, liquidity, amount0_min, amount1_min, deadline)],
    )
    return "0x" + (sel + data).hex()


def encode_collect(token_id: int, recipient: str,
                   amount0_max: int = 2**128 - 1, amount1_max: int = 2**128 - 1) -> str:
    sig = "collect((uint256,address,uint128,uint128))"
    sel = _selector(sig)
    data = encode(
        ["(uint256,address,uint128,uint128)"],
        [(token_id, recipient, amount0_max, amount1_max)],
    )
    return "0x" + (sel + data).hex()


# ── RPC helpers (allowance check) ──────────────────────────────────────────

RPC = "https://base.publicnode.com"


def _eth_call(to: str, data: str) -> str:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    })
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", RPC, "-H", "Content-Type: application/json", "-d", body],
        capture_output=True, text=True, timeout=10,
    )
    return json.loads(r.stdout).get("result", "0x")


def allowance(token: str, owner: str, spender: str) -> int:
    sel = _selector("allowance(address,address)")
    data = encode(["address", "address"], [owner, spender])
    result = _eth_call(token, "0x" + (sel + data).hex())
    return int(result, 16) if result and result != "0x" else 0


# ── Contract-call wrapper ──────────────────────────────────────────────────

def contract_call(to: str, input_data: str, amt: int = 0,
                  gas_limit: int | None = None) -> tuple[bool, str]:
    """Send a signed tx via onchainos. Returns (ok, tx_hash_or_error)."""
    args = [
        "onchainos", "wallet", "contract-call",
        "--to", to,
        "--chain", POOL_CHAIN,
        "--input-data", input_data,
        "--amt", str(amt),
        "--force",
    ]
    if gas_limit:
        args += ["--gas-limit", str(gas_limit)]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=90)
        out = r.stdout.strip()
        if r.returncode != 0:
            return False, f"exit {r.returncode}: {r.stderr[:200]}"
        data = json.loads(out)
        if not data.get("ok"):
            return False, f"not ok: {json.dumps(data)[:200]}"
        # Parse tx hash from response
        tx_hash = (data.get("data") or {}).get("txHash") or (data.get("data") or {}).get("orderId")
        return True, tx_hash or json.dumps(data)[:200]
    except Exception as e:
        return False, f"exception: {e}"


# ── High-level: ensure approval + mint ─────────────────────────────────────

MAX_UINT256 = 2**256 - 1


# ── WETH wrap/unwrap ─────────────────────────────────────────────────────

def encode_weth_deposit() -> str:
    """WETH.deposit() — wraps native ETH sent as msg.value."""
    return "0x" + _selector("deposit()").hex()


def encode_weth_withdraw(amount_wei: int) -> str:
    """WETH.withdraw(uint256) — unwraps WETH → native ETH."""
    sel = _selector("withdraw(uint256)")
    data = encode(["uint256"], [amount_wei])
    return "0x" + (sel + data).hex()


def weth_balance(owner: str = WALLET_ADDR) -> int:
    sel = _selector("balanceOf(address)")
    data = encode(["address"], [owner])
    weth_addr = TOKEN0["address"] if POOL_CHAIN == "base" else TOKEN0["address"]
    result = _eth_call(weth_addr, "0x" + (sel + data).hex())
    return int(result, 16) if result and result != "0x" else 0


def wrap_eth(amount_wei: int) -> tuple[bool, str]:
    """Call WETH.deposit() with `amount_wei` native ETH as value."""
    weth = TOKEN0["address"]
    calldata = encode_weth_deposit()
    log(f"wrap {amount_wei} ETH wei → WETH via {weth}")
    return contract_call(weth, calldata, amt=amount_wei, gas_limit=100_000)


def ensure_weth_balance(amount_wei: int) -> bool:
    """Wrap native ETH to WETH if current WETH balance < amount_wei."""
    current = weth_balance()
    if current >= amount_wei:
        return True
    need = amount_wei - current
    ok, info = wrap_eth(need)
    if not ok:
        log(f"wrap_eth failed: {info}")
    return ok


def ensure_approval(token: str, amount_min: int) -> bool:
    """Approve NPM to spend `token` if allowance < amount_min."""
    current = allowance(token, WALLET_ADDR, NPM)
    if current >= amount_min:
        log(f"allowance({token[:10]}→NPM) OK: {current}")
        return True
    log(f"approving {token[:10]} → NPM (current={current}, need={amount_min})")
    calldata = encode_approve(NPM, MAX_UINT256)
    ok, info = contract_call(token, calldata)
    log(f"approve result: {info}")
    return ok


def mint_range_order(
    side: str,               # "sell_weth" | "buy_weth"
    amount_raw: int,
    tick_lower: int,
    tick_upper: int,
    slippage_bps: int = 50,  # tolerance in basis points
) -> tuple[bool, str]:
    """Direct NPM.mint bypassing OKX router. Single-sided supported."""
    t0 = TOKEN0["address"]
    t1 = TOKEN1["address"]
    fee = int(float(CFG_FEE) * 1_000_000) if False else 3000  # Base pool 0.3%
    # Figure out which side
    if side == "sell_weth":
        # All WETH: ensure WETH balance (wrap native ETH if needed) + approve
        if not ensure_weth_balance(amount_raw):
            return False, "WETH wrap failed — check native ETH balance minus gas reserve"
        if not ensure_approval(TOKEN0["address"], amount_raw):
            return False, "WETH approval failed"
        amt0_des = amount_raw
        amt1_des = 0
    elif side == "buy_weth":
        amt0_des = 0
        amt1_des = amount_raw
        # approve USDC
        if not ensure_approval(USDC_ADDR, amount_raw):
            return False, "USDC approval failed"
    else:
        return False, f"invalid side: {side}"

    # Min amounts: tolerance vs desired
    bps = slippage_bps / 10000
    amt0_min = int(amt0_des * (1 - bps)) if amt0_des else 0
    amt1_min = int(amt1_des * (1 - bps)) if amt1_des else 0

    deadline = int(time.time()) + 600
    calldata = encode_mint(
        token0=t0, token1=t1, fee=fee,
        tick_lower=tick_lower, tick_upper=tick_upper,
        amount0_desired=amt0_des, amount1_desired=amt1_des,
        amount0_min=amt0_min, amount1_min=amt1_min,
        recipient=WALLET_ADDR, deadline=deadline,
    )
    log(f"NPM.mint direct: side={side} amt={amount_raw} range=[{tick_lower},{tick_upper}]")
    ok, info = contract_call(NPM, calldata, gas_limit=600_000)
    return ok, info


CFG_FEE = 0.003


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_mint = sub.add_parser("mint")
    p_mint.add_argument("side", choices=["buy_weth", "sell_weth"])
    p_mint.add_argument("--amount-usd", type=float, required=True)
    p_mint.add_argument("--offset-bps", type=int, default=30,
                        help="bps away from current price for range start")
    p_mint.add_argument("--width-ticks", type=int, default=2,
                        help="width in tick_spacing units")
    p_mint.add_argument("--yes", action="store_true")

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("token", choices=["usdc", "weth"])

    p_allow = sub.add_parser("allowance")

    args = ap.parse_args()

    if args.cmd == "allowance":
        a_usdc = allowance(USDC_ADDR, WALLET_ADDR, NPM)
        a_weth = allowance(TOKEN0["address"], WALLET_ADDR, NPM)
        print(f"USDC → NPM: {a_usdc}  ({a_usdc/1e6:,.2f})")
        print(f"WETH → NPM: {a_weth}  ({a_weth/1e18:,.6f})")
        return

    if args.cmd == "approve":
        token = USDC_ADDR if args.token == "usdc" else TOKEN0["address"]
        amt = MAX_UINT256
        calldata = encode_approve(NPM, amt)
        print(f"approving {args.token} → NPM (unlimited)")
        ok, info = contract_call(token, calldata)
        print(f"{'✓' if ok else '✗'} {info}")
        return

    if args.cmd == "mint":
        # Compute current tick from cl_lp
        import capital_efficiency as ce
        prices = ce.fetch_hourly_prices(INVESTMENT_ID, POOL_CHAIN)
        current_price = prices[-1][1] if prices else 0
        raw = current_price * (10 ** (int(TOKEN1["decimals"]) - int(TOKEN0["decimals"])))
        current_tick = int(math.log(raw) / math.log(1.0001))
        spacing = TICK_SPACING

        offset_ticks = int(args.offset_bps / 10000 / 0.0001)
        width = args.width_ticks * spacing

        if args.side == "buy_weth":
            # range below current
            tick_upper = ((current_tick - offset_ticks) // spacing) * spacing
            tick_lower = tick_upper - width
            amount_raw = int(args.amount_usd * (10 ** int(TOKEN1["decimals"])))
        else:
            # range above current
            tick_lower = (((current_tick + offset_ticks) + spacing - 1) // spacing) * spacing
            tick_upper = tick_lower + width
            amount_raw = int(args.amount_usd / current_price * (10 ** int(TOKEN0["decimals"])))

        p_lo = (1.0001 ** tick_lower) * (10 ** (int(TOKEN0["decimals"]) - int(TOKEN1["decimals"])))
        p_hi = (1.0001 ** tick_upper) * (10 ** (int(TOKEN0["decimals"]) - int(TOKEN1["decimals"])))
        print(f"current: tick={current_tick} price=${current_price:.2f}")
        print(f"planned range: tick [{tick_lower},{tick_upper}] price ${p_lo:.2f}-${p_hi:.2f}")
        print(f"amount: {amount_raw}  ({'USDC' if args.side=='buy_weth' else 'WETH'})")

        if not args.yes:
            if input("Proceed? (yes/NO): ").lower() != "yes":
                print("aborted"); return

        ok, info = mint_range_order(args.side, amount_raw, tick_lower, tick_upper)
        print(f"{'✓' if ok else '✗'} {info}")


if __name__ == "__main__":
    _cli()
