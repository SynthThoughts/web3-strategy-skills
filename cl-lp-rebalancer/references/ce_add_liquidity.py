#!/usr/bin/env python3
"""Add idle wallet funds to an existing V3 position (no new NFT).

Reads current wallet (native USDC + ETH beyond gas reserve), balances them
at the position's tick range, swaps if needed, then calls defi_deposit with
--token-id to add liquidity to the existing NFT.

Usage: ce_add_liquidity.py <target_token_id> [lark_webhook]
"""
import sys
import os
import json
import time
import subprocess
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cl_lp import (
    get_balances, get_eth_price,
    _calc_balanced_deposit, _enforce_gas_reserve, defi_deposit,
    get_position_detail, load_state, save_state,
    GAS_RESERVE_ETH,
)


def refresh_snapshot_via_tick():
    """Run cl_lp.py tick so _cached_snapshot is freshened for dashboard."""
    try:
        subprocess.run(
            ["python3", "cl_lp.py", "tick"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=60, check=False,
            capture_output=True,
        )
    except Exception as e:
        print(f"[WARN] tick refresh failed: {e}")

TOKEN_ID = sys.argv[1]
LARK = sys.argv[2] if len(sys.argv) > 2 else None


def notify(title, content):
    line = f"[{time.strftime('%H:%M:%S')}] === {title} ===\n{content}\n"
    print(line, flush=True)
    if not LARK:
        return
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"CL-LP · {title}"},
                       "template": "blue"},
            "elements": [{"tag": "markdown", "content": content}],
        },
    }
    try:
        req = urllib.request.Request(
            LARK, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[LARK ERROR] {e}")


def main():
    # Read position range from state (must match TOKEN_ID)
    state = load_state()
    pos = state.get("position") or {}
    if pos.get("token_id") != TOKEN_ID:
        notify("❌ token_id 不匹配",
               f"state 里是 `{pos.get('token_id')}`，要求 `{TOKEN_ID}`。中止")
        return
    tick_lo = pos["tick_lower"]
    tick_hi = pos["tick_upper"]

    # Pre-flight
    eth, usdc, fail = get_balances(force=True)
    if fail:
        notify("❌ Balance 查询失败", "")
        return
    price = get_eth_price() or 0
    pre_detail = get_position_detail(TOKEN_ID)
    pre_lp = pre_detail.get("value", 0)

    notify("🔧 补仓启动", f"""**目标**: 补仓到 token_id=`{TOKEN_ID}`（不新建 NFT）
**仓位 range**: tick `[{tick_lo}, {tick_hi}]`
**当前 LP**: `${pre_lp:.2f}`
**钱包**: `{eth:.6f}` ETH + `${usdc:.2f}` USDC
**ETH 现价**: `${price:.2f}`
**Gas reserve**: `{GAS_RESERVE_ETH}` ETH 必须保留

可用 ETH: `{max(eth - GAS_RESERVE_ETH, 0):.6f}`
预期将 swap 一部分 USDC → ETH 以配平 range ratio""")

    available_eth = max(eth - GAS_RESERVE_ETH, 0)
    if available_eth * price + usdc < 10:
        notify("⏭ 金额太小", f"总可投入 < $10，跳过")
        return

    # Calculate balanced deposit (this internally runs probe → swap → deposit calc)
    notify("Step 1/2 · 配平 (swap USDC→ETH)",
           f"跑 probe → target ratio → swap 调整")
    user_input = _calc_balanced_deposit(available_eth, usdc, price, tick_lo, tick_hi)
    if not user_input:
        notify("❌ 配平失败", "`_calc_balanced_deposit` 返回 None")
        return
    user_input = _enforce_gas_reserve(user_input)
    if not user_input:
        notify("❌ Gas reserve guard 拒绝", "钱包 ETH 不够 reserve + deposit")
        return

    try:
        items = json.loads(user_input)
        summary = []
        for it in items:
            addr = it.get("tokenAddress", "")[:10] + "…"
            amt = float(it.get("coinAmount", 0))
            prec = int(it.get("tokenPrecision", 0))
            summary.append(f"- `{addr}`: `{amt / (10 ** prec):.6f}`")
        notify("✓ 配平就绪", "deposit 输入:\n" + "\n".join(summary))
    except Exception:
        pass

    # Deposit via --token-id (add liquidity, no new NFT)
    notify("Step 2/2 · 添加流动性 (--token-id, 不新建)",
           f"对 `{TOKEN_ID}` 调用 defi deposit --token-id")
    ok = defi_deposit(user_input, tick_lo, tick_hi, token_id=TOKEN_ID)
    if not ok:
        notify("❌ Deposit 失败", "调用返回 False")
        return

    # Verify
    time.sleep(20)
    post_detail = get_position_detail(TOKEN_ID)
    post_lp = post_detail.get("value", 0)
    post_eth, post_usdc, _ = get_balances(force=True)
    gas_ok = "✅" if post_eth >= GAS_RESERVE_ETH else "❌ 违反 0.02 守卫!"

    # Peak stays at pre-rebalance portfolio (add-liquidity doesn't change total
    # other than gas/slippage loss). Avoids trusting glitchy post-deposit balance.
    pre_portfolio = eth * price + usdc + pre_lp
    state.setdefault("stats", {})["portfolio_peak_usd"] = round(pre_portfolio, 2)
    state["_value_history"] = [round(pre_portfolio, 2)]
    save_state(state)

    notify("✅ 补仓完成", f"""**token_id**: `{TOKEN_ID}` (未变)
**LP 增长**: `${pre_lp:.2f}` → `${post_lp:.2f}` (+`${post_lp - pre_lp:.2f}`)
**钱包剩余**: `{post_eth:.6f}` ETH (≥ `{GAS_RESERVE_ETH}` {gas_ok}) + `${post_usdc:.2f}` USDC
**Peak 固定**: `${pre_portfolio:.2f}` (rebalance 前基准,避免 API glitch)""")

    # Refresh _cached_snapshot so dashboard sees new LP value
    time.sleep(15)
    refresh_snapshot_via_tick()


if __name__ == "__main__":
    main()
