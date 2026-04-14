#!/usr/bin/env python3
"""Offline pool recommender: scan same-risk-tier pools, score via CE model,
suggest switch iff a candidate sustained >threshold uplift for ≥2 consecutive
hourly runs.

Per-pool CE score = share × TIR × apy_3h − expected_rebalance_cost
(same model as capital_efficiency.find_best_range, but using PoolConfig so
the math works for arbitrary V3 pools).

Persistence:
  pool_selector_state.json — tracks each candidate's uplift streak so we only
  recommend switches that survived 2 independent samples (debounced).

Usage:
  pool_selector.py [--capital 500] [--max-risk medium] [--chain base] \
                   [--threshold 0.30] [--lark <webhook>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import capital_efficiency as ce
from pool_config import PoolConfig, fetch_pool_config
from pool_compare import search_pools, score as classify_pool
from token_registry import risk_tier, allowed

_INSTANCE_DIR = Path(os.environ.get("LP_AUTO_INSTANCE_DIR", str(Path(__file__).parent)))
SELECTOR_STATE = _INSTANCE_DIR / "pool_selector_state.json"
STREAK_HOURS_REQUIRED = 2       # ≥2 consecutive leading runs before recommending
REBALANCE_COST_USD = 5.0
SWITCH_COST_USD = 12.0          # cross-pool switch: close + swap + mint ≈ $12
MIN_APY = 0.10


def score_pool(cfg: PoolConfig, capital_usd: float,
               atr_pct_1h: float = 3.0) -> dict | None:
    """Run CE optimizer on one pool, return summary with best range + net_24h."""
    try:
        depth = ce.fetch_depth(cfg.investment_id, cfg.chain)
        prices = ce.fetch_hourly_prices(cfg.investment_id, cfg.chain)
        rates = ce.fetch_hourly_rates(cfg.investment_id, cfg.chain)
    except Exception as e:
        return {"pool_id": cfg.investment_id, "error": f"fetch: {e}"}

    if not prices or not rates:
        return {"pool_id": cfg.investment_id, "error": "empty data"}
    current_price = prices[-1][1]
    apy_3h = ce.recent_apy(rates, 3)
    if apy_3h < MIN_APY:
        return {"pool_id": cfg.investment_id, "apy_3h": apy_3h,
                "status": "gated", "net_24h": 0}

    total_24h_fee = sum(r for _, _, r in rates)
    best = None
    for mult in [0.1, 0.3, 0.8, 1.2, 1.6]:
        half = max(atr_pct_1h * mult, 0.05)
        for off in [-20, -10, 0, 10, 20]:
            center = current_price * (1 + off / 100)
            lo, hi = center * (1 - half / 100), center * (1 + half / 100)
            if not (lo <= current_price <= hi):
                continue
            t_lo = (cfg.price_usd_to_tick(lo) // cfg.tick_spacing) * cfg.tick_spacing
            t_hi = ((cfg.price_usd_to_tick(hi) + cfg.tick_spacing - 1) // cfg.tick_spacing) * cfg.tick_spacing
            if t_hi <= t_lo:
                continue
            p_lo, p_hi = cfg.tick_to_price_usd(t_lo), cfg.tick_to_price_usd(t_hi)
            if p_lo > p_hi:
                p_lo, p_hi = p_hi, p_lo

            my_L = cfg.calc_my_L(capital_usd, current_price, p_lo, p_hi)
            avg_L = ce.avg_L_in_range(depth, t_lo, t_hi)
            share = my_L / (avg_L + my_L) if (avg_L + my_L) > 0 else 0.0
            tir = ce.time_in_range(prices, p_lo, p_hi)
            # Fee captured while price in range (simplified: use total × TIR)
            fee_cap_24h = total_24h_fee * tir
            expected_fee = fee_cap_24h * share

            # Rebalance cost: 24h path length / width × $5
            path = sum(abs(prices[i][1] - prices[i - 1][1]) for i in range(1, len(prices)))
            width = p_hi - p_lo
            n_reb = int(path / width) if width > 0 else 999
            cost = n_reb * REBALANCE_COST_USD
            net_24h = expected_fee - cost
            if best is None or net_24h > best["net_24h"]:
                best = {
                    "price_lo": p_lo, "price_hi": p_hi,
                    "width_pct": (p_hi - p_lo) / current_price * 100,
                    "tick_lo": t_lo, "tick_hi": t_hi,
                    "share": share, "tir": tir,
                    "fee_24h": expected_fee,
                    "n_reb": n_reb, "cost_24h": cost,
                    "net_24h": net_24h,
                }

    if not best or best["net_24h"] <= 0:
        return {"pool_id": cfg.investment_id, "apy_3h": apy_3h,
                "status": "negative_net", "net_24h": best["net_24h"] if best else 0}

    return {
        "pool_id": cfg.investment_id,
        "pair": f"{cfg.token0_symbol}/{cfg.token1_symbol}",
        "fee": cfg.fee_tier,
        "apy_3h": apy_3h,
        "status": "ok",
        "current_price": current_price,
        **best,
    }


def load_selector_state() -> dict:
    if SELECTOR_STATE.exists():
        try:
            return json.loads(SELECTOR_STATE.read_text())
        except Exception:
            pass
    return {"runs": []}


def save_selector_state(state: dict):
    SELECTOR_STATE.write_text(json.dumps(state, indent=2))


def notify_lark(webhook: str, title: str, content: str):
    body = {"msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title},
                           "template": "orange"},
                "elements": [{"tag": "markdown", "content": content}],
            }}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[LARK ERROR] {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=450.0)
    ap.add_argument("--max-risk", default="medium")
    ap.add_argument("--chain", default="base")
    ap.add_argument("--threshold", type=float, default=0.30,
                    help="fractional uplift over current required (0.30 = 30%%)")
    ap.add_argument("--current-pool", default="326890603",
                    help="current pool investmentId")
    ap.add_argument("--atr-1h", type=float, default=3.0,
                    help="current 1h ATR%% (affects range width)")
    ap.add_argument("--tokens", default="USDC,ETH,BTC,WBTC,cbBTC,cbETH,DAI,USDT")
    ap.add_argument("--lark", default="")
    args = ap.parse_args()

    # ── 1. Discover pools & filter by risk tier ─────────────────────────
    print(f"Scanning {args.chain} pools...")
    raw = search_pools(args.chain, args.tokens.split(","))
    pools = [classify_pool(p) for p in raw]
    pools = [p for p in pools if p["id"] and allowed(p["tier"], args.max_risk)]
    print(f"  {len(pools)} pools in tier ≤ {args.max_risk}")

    # ── 2. CE-score each pool (incl. current) ────────────────────────────
    scored = []
    for p in pools:
        print(f"  scoring {p['id']} {p['pair']} ({p['tier']})...")
        cfg = fetch_pool_config(p["id"], args.chain)
        if not cfg:
            continue
        s = score_pool(cfg, args.capital, args.atr_1h)
        s["name"] = p["pair"]
        s["tier"] = p["tier"]
        scored.append(s)

    ok = [s for s in scored if s.get("status") == "ok"]
    ok.sort(key=lambda x: -x["net_24h"])

    # ── 3. Report ────────────────────────────────────────────────────────
    print()
    print(f"{'id':>10} {'pair':>14} {'tier':<12} {'net/24h':>10} {'fee':>8} {'share':>8} {'TIR':>5} {'#reb':>4}")
    for s in ok[:10]:
        print(f"{s['pool_id']:>10} {s['name']:>14} {s['tier']:<12} ${s['net_24h']:>8.2f} ${s['fee_24h']:>6.2f} {s['share']*100:>7.4f}% {s['tir']*100:>4.0f}% {s['n_reb']:>4}")

    # Identify current + best candidate
    current = next((s for s in ok if s["pool_id"] == args.current_pool), None)
    top = ok[0] if ok else None
    if not current:
        print(f"\n⚠ Current pool {args.current_pool} not in scored set (gated or failed).")
        return
    if not top or top["pool_id"] == args.current_pool:
        print(f"\n✓ Current pool is already the top scorer.")
        return

    uplift = (top["net_24h"] - current["net_24h"]) / max(current["net_24h"], 0.01)
    print(f"\n候选 #1: {top['name']} {top['pool_id']}  net/24h ${top['net_24h']:.2f}")
    print(f"当前:    {current['name']} {current['pool_id']}  net/24h ${current['net_24h']:.2f}")
    print(f"Uplift: {uplift*100:.1f}%  (threshold {args.threshold*100:.0f}%)")
    print(f"扣切换成本 ${SWITCH_COST_USD}: net uplift ${top['net_24h'] - current['net_24h'] - SWITCH_COST_USD:.2f}")

    if uplift < args.threshold:
        print("→ Below threshold, no action.")
        return

    # ── 4. Persist streak: require ≥2 consecutive runs before recommending ─
    state = load_selector_state()
    runs = state.get("runs", [])
    runs.append({
        "ts": datetime.utcnow().isoformat(),
        "current": args.current_pool,
        "leader": top["pool_id"],
        "uplift": round(uplift, 4),
    })
    runs = runs[-24:]  # keep last 24 runs
    state["runs"] = runs
    save_selector_state(state)

    # Streak: count consecutive recent runs where leader == top["pool_id"]
    streak = 0
    for run in reversed(runs):
        if run["leader"] == top["pool_id"] and run["uplift"] >= args.threshold:
            streak += 1
        else:
            break

    print(f"\nStreak: {streak} consecutive runs with {top['name']} leading (≥{args.threshold*100:.0f}% uplift)")
    print(f"Required: {STREAK_HOURS_REQUIRED}")

    if streak < STREAK_HOURS_REQUIRED:
        print(f"→ Waiting for {STREAK_HOURS_REQUIRED - streak} more confirmation(s) before recommending switch.")
        # Clear stale recommendation
        state.pop("recommend", None)
        save_selector_state(state)
        return

    # Persist recommendation so `lp-auto switch` / auto_switch can act on it
    state["recommend"] = {
        "pool_id": top["pool_id"],
        "pair": top["name"],
        "tick_lo": top["tick_lo"],
        "tick_hi": top["tick_hi"],
        "price_lo": top["price_lo"],
        "price_hi": top["price_hi"],
        "net_24h": top["net_24h"],
        "current_net_24h": current["net_24h"],
        "uplift": uplift,
        "streak": streak,
        "recommended_at": datetime.utcnow().isoformat(),
    }
    save_selector_state(state)

    msg = f"""**Recommendation**: switch to `{top['name']}` (id={top['pool_id']})

| | Current | Candidate |
|---|---|---|
| Pool | {current['name']} ({current['pool_id']}) | {top['name']} ({top['pool_id']}) |
| net / 24h | ${current['net_24h']:.2f} | **${top['net_24h']:.2f}** |
| uplift | — | **{uplift*100:.1f}%** (sustained {streak} hours) |
| switch cost | — | ${SWITCH_COST_USD} |
| net uplift after cost | — | **${top['net_24h'] - current['net_24h'] - SWITCH_COST_USD:.2f}/24h** |

Target range: `${top['price_lo']:.0f}-${top['price_hi']:.0f}` (width {top['width_pct']:.2f}%)
Target tick: `[{top['tick_lo']}, {top['tick_hi']}]`

Execute via: `lp-auto switch`"""
    print(f"\n{'='*60}")
    print("🎯 RECOMMEND SWITCH (persisted)")
    print('='*60)
    print(msg)

    if args.lark:
        notify_lark(args.lark, f"Pool switch recommended → {top['name']}", msg)


if __name__ == "__main__":
    main()
