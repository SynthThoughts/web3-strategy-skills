#!/usr/bin/env python3
"""One-shot CE rebalance: close → balance → deposit at given tick range.
Each step posts to Lark + stdout.

Usage:
  ce_execute.py <tick_lo> <tick_hi> [lark_webhook]
"""
import sys
import os
import json
import time
import subprocess
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cl_lp import (
    defi_claim_fees, defi_redeem, get_balances, get_eth_price,
    _calc_balanced_deposit, _enforce_gas_reserve, defi_deposit,
    _query_all_positions, get_position_detail,
    load_state, save_state, log,
    GAS_RESERVE_ETH,
)


def refresh_snapshot_via_tick():
    """Run cl_lp.py tick in subprocess so _cached_snapshot is freshened for dashboard."""
    try:
        subprocess.run(
            ["python3", "cl_lp.py", "tick"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=60, check=False,
            capture_output=True,
        )
    except Exception as e:
        print(f"[WARN] tick refresh failed: {e}")

TICK_LO = int(sys.argv[1])
TICK_HI = int(sys.argv[2])
LARK = sys.argv[3] if len(sys.argv) > 3 else None


def notify(title, content):
    line = f"[{time.strftime('%H:%M:%S')}] === {title} ===\n{content}\n"
    print(line, flush=True)
    if not LARK:
        return
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"CL-LP · {title}"},
                       "template": "turquoise"},
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


def tick_to_price(t):
    import math
    raw = 1.0001 ** t
    return raw * 1e12


def main():
    p_lo = tick_to_price(TICK_LO)
    p_hi = tick_to_price(TICK_HI)
    state = load_state()
    pos = state.get("position") or {}
    token_id = pos.get("token_id")

    # ─ Pre-flight ──────────────────────────────────────────────────────
    pre_eth, pre_usdc, _ = get_balances(force=True)
    price = get_eth_price() or 0
    lp_val = 0
    if token_id:
        try:
            lp_val = get_position_detail(token_id).get("value", 0)
        except Exception:
            pass
    total = pre_eth * price + pre_usdc + lp_val

    notify("🚀 Rebalance 启动", f"""**当前持仓**
- 旧 token_id: `{token_id or "(none)"}`  LP=`${lp_val:.2f}`
- 钱包: `{pre_eth:.6f}` ETH + `${pre_usdc:.2f}` USDC
- ETH 现价: `${price:.2f}`  组合总值: `${total:.2f}`

**目标新仓**
- Tick range: `[{TICK_LO}, {TICK_HI}]`
- 价格区间: `${p_lo:.0f} - ${p_hi:.0f}` (宽 {(p_hi-p_lo)/price*100:.2f}%)
- Gas reserve: `{GAS_RESERVE_ETH}` ETH 必须保留""")

    # ─ Step 1: Claim + Redeem ─────────────────────────────────────────
    if token_id:
        notify("Step 1/3 · 赎回", f"对 token_id=`{token_id}` 执行 claim fees + redeem (decreaseLiquidity + collect + burn)")
        try:
            pre_claim = get_position_detail(token_id)
            unclaimed = pre_claim.get("unclaimed_fee_usd", 0)
            if unclaimed > 0:
                defi_claim_fees(token_id)
        except Exception as e:
            print(f"claim failed: {e}")
        ok = defi_redeem(token_id)
        if not ok:
            notify("❌ Redeem 失败", f"token_id=`{token_id}` 未成功，中止流程")
            return
        # Wait for settlement
        settled = False
        for i in range(10):
            time.sleep(10)
            eth2, usdc2, bf = get_balances(force=True)
            if bf: continue
            if eth2 > pre_eth + 0.001 or usdc2 > pre_usdc + 2:
                settled = True
                break
        if not settled:
            notify("⚠ Redeem settlement 超时", "balance 未见增加，可能需人工检查")
            return
        notify("✓ Step 1 完成", f"赎回后钱包: `{eth2:.6f}` ETH + `${usdc2:.2f}` USDC")
        cur_eth, cur_usdc = eth2, usdc2
    else:
        cur_eth, cur_usdc = pre_eth, pre_usdc
        notify("Step 1/3 · 跳过", "无活跃仓位，直接进入配平")

    # ─ Step 2: Calc + auto-swap to balance ────────────────────────────
    available_eth = max(cur_eth - GAS_RESERVE_ETH, 0)
    price = get_eth_price() or price
    notify("Step 2/3 · 配平",
           f"""**入参**
- 可用 ETH: `{available_eth:.6f}` (已扣 `{GAS_RESERVE_ETH}` gas reserve)
- USDC: `${cur_usdc:.2f}`
- ETH 现价: `${price:.2f}`

_将执行 probe → target ratio → swap ETH↔USDC 到新 range 所需比例_""")
    user_input = _calc_balanced_deposit(
        available_eth, cur_usdc, price, TICK_LO, TICK_HI,
    )
    if not user_input:
        notify("❌ 配平失败", "`_calc_balanced_deposit` 返回 None，资金留在钱包")
        return
    user_input = _enforce_gas_reserve(user_input)
    if not user_input:
        notify("❌ Gas reserve guard 拒绝", "钱包 ETH 不够 reserve + deposit")
        return

    # Show what will be deposited
    try:
        items = json.loads(user_input)
        summary = []
        for it in items:
            addr = it.get("tokenAddress", "")[:10] + "…"
            amt = float(it.get("coinAmount", 0))
            prec = int(it.get("tokenPrecision", 0))
            summary.append(f"- `{addr}`: `{amt / (10 ** prec):.6f}`")
        notify("✓ Step 2 完成 · 配平就绪", "deposit 输入:\n" + "\n".join(summary))
    except Exception:
        pass

    # ─ Step 3: Deposit at new range ───────────────────────────────────
    pre_tids = set(p["tokenId"] for p in (_query_all_positions() or []))
    notify("Step 3/3 · 存入新 range", f"对 tick `[{TICK_LO}, {TICK_HI}]` 发起 V3 mint")
    ok = defi_deposit(user_input, TICK_LO, TICK_HI)
    if not ok:
        notify("❌ Deposit 失败", "返回 False，资金可能部分在 swap 完的钱包里")
        return

    # Verify new token_id
    new_tid = None
    new_val = 0.0
    for i in range(10):
        time.sleep(8)
        post_tids = set(p["tokenId"] for p in (_query_all_positions() or []))
        diff = post_tids - pre_tids
        if diff:
            new_tid = sorted(diff, key=lambda x: int(x))[-1]
            try:
                new_val = get_position_detail(new_tid).get("value", 0)
            except Exception:
                pass
            break

    final_eth, final_usdc, _ = get_balances(force=True)
    gas_ok = "✅" if final_eth >= GAS_RESERVE_ETH else "❌ 违反 0.02 ETH 守卫!"

    # Update state with new position
    if new_tid:
        state["position"] = {
            "token_id": new_tid,
            "tick_lower": TICK_LO,
            "tick_upper": TICK_HI,
            "lower_price": round(p_lo, 2),
            "upper_price": round(p_hi, 2),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000"),
            "entry_price": round(price, 2),
        }
        # Peak: use pre-rebalance portfolio (rebalance only loses gas/slippage/IL,
        # never gains). Avoids trusting possibly-glitched post-deposit balance reads.
        pre_portfolio = pre_eth * price + pre_usdc + lp_val
        state.setdefault("stats", {})["portfolio_peak_usd"] = round(pre_portfolio, 2)
        state["_value_history"] = [round(pre_portfolio, 2)]
        save_state(state)

    # Refresh _cached_snapshot so dashboard sees new LP value immediately
    time.sleep(15)  # let OKX balance API settle
    refresh_snapshot_via_tick()

    notify("✅ Rebalance 完成", f"""**新仓位**
- token_id: `{new_tid or "(未确认)"}`
- Tick range: `[{TICK_LO}, {TICK_HI}]`
- 价格区间: `${p_lo:.0f} - ${p_hi:.0f}`
- LP 价值: `${new_val:.2f}`

**钱包结余**
- ETH: `{final_eth:.6f}` (≥ `{GAS_RESERVE_ETH}` {gas_ok})
- USDC: `${final_usdc:.2f}`

**组合总值**: `${final_eth * price + final_usdc + new_val:.2f}`""")


if __name__ == "__main__":
    main()
