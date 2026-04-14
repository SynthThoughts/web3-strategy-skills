#!/usr/bin/env python3
"""Cross-pool LP migration: close current position, bridge tokens via USDC,
deposit into a different V3 pool.

Flow:
  1. Read current state.position → close via defi_redeem
  2. Wait for settlement → inventory wallet balances
  3. If target pool uses different tokens: swap everything non-target → USDC,
     then use the target pool's own calculate-entry to do final ratio swap.
  4. Reserve 0.02 ETH (gas) before all swaps/deposits.
  5. defi_deposit with new pool's investment_id + tick range → mint new NFT
  6. Update state with new position + pool_config
  7. Lark notify each step.

Assumptions (v1):
  - Both current and target pools include at least one stable token (USDC).
    This lets us use USDC as the bridge asset without extra cross-swaps.
  - Chain stays the same (no cross-chain bridging).

Usage:
  pool_switch.py <target_investment_id> <tick_lo> <tick_hi> [--dry-run] [--lark <webhook>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cl_lp import (
    defi_claim_fees, defi_redeem, get_balances, get_eth_price,
    defi_deposit, _query_all_positions, get_position_detail,
    execute_swap, load_state, save_state,
    GAS_RESERVE_ETH, NATIVE_TOKEN, USDC_ADDR, onchainos_cmd,
    INVESTMENT_ID as CURRENT_INVESTMENT_ID, WALLET_ADDR, POOL_CHAIN,
)
from pool_config import fetch_pool_config, PoolConfig


def notify(webhook: str, title: str, content: str):
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] === {title} ===\n{content}\n", flush=True)
    if not webhook:
        return
    body = {"msg_type": "interactive",
            "card": {"header": {"title": {"tag": "plain_text", "content": f"CL-LP · {title}"},
                                "template": "purple"},
                     "elements": [{"tag": "markdown", "content": content}]}}
    try:
        req = urllib.request.Request(webhook, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[LARK ERROR] {e}")


def wait_settlement(pre_eth: float, pre_usdc: float, max_s: int = 120) -> tuple[float, float]:
    """Poll balance until redeem settles (ETH or USDC grew meaningfully)."""
    for i in range(max_s // 10):
        time.sleep(10)
        eth, usdc, fail = get_balances(force=True)
        if fail:
            continue
        if eth > pre_eth + 0.001 or usdc > pre_usdc + 2:
            return eth, usdc
    return get_balances(force=True)[:2]


def do_swap_to_usdc(eth_amount: float, price: float) -> bool:
    """Swap ETH (minus gas reserve) → USDC. Returns True on success."""
    if eth_amount <= 0:
        return True
    amount_wei = int(eth_amount * 10**18)
    tx, fail = execute_swap(NATIVE_TOKEN, USDC_ADDR, amount_wei, price)
    if not tx:
        print(f"  swap ETH→USDC failed: {fail}")
        return False
    time.sleep(15)
    return True


def target_pool_calc_entry(target_cfg: PoolConfig, usdc_amount: float,
                            tick_lo: int, tick_hi: int) -> str | None:
    """Use target pool's calculate-entry with USDC as input to get required
    dual-token breakdown. Returns JSON string ready for defi_deposit, or None."""
    usdc_raw = int(usdc_amount * 0.95 * (10 ** target_cfg.token1_decimals))  # 5% safety
    # Determine which token is USDC in the target pool
    if target_cfg.token0_symbol == "USDC":
        input_token = target_cfg.token0_address
        input_decimal = target_cfg.token0_decimals
    elif target_cfg.token1_symbol == "USDC":
        input_token = target_cfg.token1_address
        input_decimal = target_cfg.token1_decimals
        usdc_raw = int(usdc_amount * 0.95 * (10 ** target_cfg.token1_decimals))
    else:
        print("  target pool has no USDC leg — cannot bridge via USDC")
        return None

    result = onchainos_cmd([
        "defi", "calculate-entry",
        "--investment-id", target_cfg.investment_id,
        "--chain", target_cfg.chain,
        "--input-token", input_token,
        "--input-amount", str(usdc_raw),
        "--token-decimal", str(input_decimal),
        "--tick-lower", str(tick_lo),
        "--tick-upper", str(tick_hi),
    ], timeout=30)
    if not result or not result.get("ok"):
        print(f"  calculate-entry failed: {result}")
        return None
    # result["data"] is a list of {tokenAddress, coinAmount, tokenPrecision, ...}
    return json.dumps(result.get("data", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target_id", help="target pool investmentId")
    ap.add_argument("tick_lo", type=int)
    ap.add_argument("tick_hi", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lark", default="")
    args = ap.parse_args()

    target_cfg = fetch_pool_config(args.target_id, POOL_CHAIN)
    if not target_cfg:
        print(f"❌ fetch_pool_config({args.target_id}) failed")
        sys.exit(1)

    state = load_state()
    pos = state.get("position") or {}
    old_tid = pos.get("token_id")

    pre_eth, pre_usdc, _ = get_balances(force=True)
    price = get_eth_price() or 0
    pre_lp = get_position_detail(old_tid).get("value", 0) if old_tid else 0

    notify(args.lark, "🔀 Pool Switch 启动", f"""**从**: pool `{CURRENT_INVESTMENT_ID}` token_id=`{old_tid}` LP=`${pre_lp:.2f}`
**到**: pool `{target_cfg.investment_id}` `{target_cfg.token0_symbol}/{target_cfg.token1_symbol}` fee=`{target_cfg.fee_tier}`
**Tick range**: `[{args.tick_lo}, {args.tick_hi}]`
**Dry-run**: {args.dry_run}
**钱包 pre**: `{pre_eth:.6f}` ETH + `${pre_usdc:.2f}` USDC  总值: `${pre_eth*price + pre_usdc + pre_lp:.2f}`""")

    if args.dry_run:
        print("--- DRY RUN: skipping on-chain actions ---")
        print(f"Would: redeem {old_tid}, swap non-USDC → USDC, calculate-entry on "
              f"pool {args.target_id}, then defi_deposit at ticks [{args.tick_lo},{args.tick_hi}]")
        return

    # ─ Step 1: Close current ──────────────────────────────────────────
    if old_tid:
        notify(args.lark, "Step 1/4 · 关闭旧仓", f"claim + redeem token `{old_tid}`")
        try:
            defi_claim_fees(old_tid)
        except Exception:
            pass
        if not defi_redeem(old_tid):
            notify(args.lark, "❌ Redeem 失败", "中止")
            return
        eth_after, usdc_after = wait_settlement(pre_eth, pre_usdc)
        notify(args.lark, "✓ Step 1 完成",
               f"钱包: `{eth_after:.6f}` ETH + `${usdc_after:.2f}` USDC")
    else:
        eth_after, usdc_after = pre_eth, pre_usdc

    # ─ Step 2: Bridge non-USDC tokens → USDC ──────────────────────────
    # If current pool held ETH and target has no ETH, swap ETH → USDC
    # (respecting gas reserve). Keeps implementation simple; assumes both
    # pools include USDC.
    available_eth = max(eth_after - GAS_RESERVE_ETH, 0)
    target_has_eth = target_cfg.token0_symbol in ("WETH", "ETH") or \
                     target_cfg.token1_symbol in ("WETH", "ETH")
    price = get_eth_price() or price

    if not target_has_eth and available_eth * price > 5:
        notify(args.lark, "Step 2/4 · 桥接 ETH→USDC",
               f"target 池无 ETH，将 `{available_eth:.6f}` ETH 换成 USDC")
        if not do_swap_to_usdc(available_eth, price):
            notify(args.lark, "❌ Bridge swap 失败", "中止")
            return
        eth_after, usdc_after, _ = get_balances(force=True)
        notify(args.lark, "✓ Step 2 完成",
               f"钱包: `{eth_after:.6f}` ETH + `${usdc_after:.2f}` USDC")
    else:
        notify(args.lark, "Step 2/4 · 桥接跳过", "target 池包含 ETH leg，无需 bridge swap")

    # ─ Step 3: Build deposit input via target pool's calculate-entry ──
    notify(args.lark, "Step 3/4 · 配平 (target pool calculate-entry)",
           f"用 `${usdc_after * 0.95:.2f}` USDC 作为 input 算出双币比例")
    user_input = target_pool_calc_entry(target_cfg, usdc_after, args.tick_lo, args.tick_hi)
    if not user_input:
        notify(args.lark, "❌ calculate-entry 失败", "中止")
        return

    # Summary
    try:
        items = json.loads(user_input)
        summary = []
        for it in items:
            addr = it.get("tokenAddress", "")[:10] + "…"
            amt = float(it.get("coinAmount", 0))
            prec = int(it.get("tokenPrecision", 0))
            summary.append(f"- `{addr}`: `{amt / (10 ** prec):.6f}`")
        notify(args.lark, "✓ Step 3 完成", "deposit 输入:\n" + "\n".join(summary))
    except Exception:
        pass

    # ─ Step 4: Deposit into target pool ───────────────────────────────
    notify(args.lark, "Step 4/4 · mint 新 NFT on target pool",
           f"pool `{target_cfg.investment_id}` tick `[{args.tick_lo}, {args.tick_hi}]`")

    # defi_deposit uses module-level INVESTMENT_ID; we need to override. The
    # cleanest way is to call onchainos directly here.
    args_cmd = [
        "defi", "deposit",
        "--investment-id", target_cfg.investment_id,
        "--address", WALLET_ADDR,
        "--chain", target_cfg.chain,
        "--user-input", user_input,
        "--tick-lower", str(args.tick_lo),
        "--tick-upper", str(args.tick_hi),
    ]
    result = onchainos_cmd(args_cmd, timeout=60)
    if not (result and result.get("ok")):
        notify(args.lark, "❌ Deposit 失败", json.dumps(result)[:300])
        return
    # Broadcast via cl_lp's helper
    from cl_lp import _broadcast_defi_txs
    if not _broadcast_defi_txs(result, "deposit"):
        notify(args.lark, "❌ Broadcast 失败", "")
        return

    time.sleep(20)

    # Find new token_id in target pool
    post_tids = [p for p in (_query_all_positions() or [])
                 if str(p.get("investmentId", "")) == target_cfg.investment_id]
    new_tid = sorted([p["tokenId"] for p in post_tids], key=lambda x: int(x))[-1] if post_tids else None
    new_lp = get_position_detail(new_tid).get("value", 0) if new_tid else 0
    final_eth, final_usdc, _ = get_balances(force=True)

    # Update state: new pool + new position
    if new_tid:
        state["position"] = {
            "token_id": new_tid,
            "tick_lower": args.tick_lo,
            "tick_upper": args.tick_hi,
            "investment_id": target_cfg.investment_id,
            "chain": target_cfg.chain,
            "token0_symbol": target_cfg.token0_symbol,
            "token1_symbol": target_cfg.token1_symbol,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000"),
        }
        total = final_eth * price + final_usdc + new_lp
        state.setdefault("stats", {})["portfolio_peak_usd"] = round(total, 2)
        state["_value_history"] = [round(total, 2)]
        save_state(state)

    gas_ok = "✅" if final_eth >= GAS_RESERVE_ETH else "❌ 违反 0.02 ETH 守卫!"
    notify(args.lark, "✅ Pool Switch 完成", f"""**新池**: `{target_cfg.investment_id}` {target_cfg.token0_symbol}/{target_cfg.token1_symbol}
**新仓**: token_id=`{new_tid}` LP=`${new_lp:.2f}`
**钱包**: `{final_eth:.6f}` ETH {gas_ok} + `${final_usdc:.2f}` USDC
**组合总值**: `${final_eth * price + final_usdc + new_lp:.2f}`

⚠ 注意: state.json 里的 investment_id 和 token_id 都换了新池的。如需回滚,从 state.json.bak 恢复""")


if __name__ == "__main__":
    main()
