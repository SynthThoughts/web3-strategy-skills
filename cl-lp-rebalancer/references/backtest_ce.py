"""Offline backtest of capital-efficiency optimizer vs legacy ATR×regime.

Replays the last 7 days at 6h intervals. For each snapshot:
  - Fetch "then" state: current_price, apy, ATR from past 14h, price history 24h
  - Run NEW strategy (find_best_range) → picks (tick_lo, tick_hi)
  - Run LEGACY strategy (ATR × regime mult) → picks (tick_lo, tick_hi)
  - Replay next 6h of hourly prices + fees; compute realized_fee = Σ(fee_h × 1{p_h ∈ range}) × share

Limitations:
  - Depth is current snapshot only (no historical); we assume depth was similar
  - Rate-chart WEEK is 6h granularity → use as-is for per-6h apy_3h proxy
  - No gas / IL cost, no rebalance frequency — pure fee capture comparison
"""
import json
import subprocess
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import capital_efficiency as ce

CAPITAL_USD = 454.0
CHAIN = "base"
INVESTMENT_ID = "326890603"

# Legacy ATR × regime multipliers (from cl_lp.py config.json)
LEGACY_RANGE_MULT = {"low": 0.5, "medium": 0.8, "high": 1.2, "extreme": 1.5}
LEGACY_VOL_THRESHOLDS = {"medium": 5.0, "high": 8.0, "extreme": 100.0}


def legacy_regime(atr_pct: float) -> str:
    if atr_pct < 2.0: return "low"
    if atr_pct < LEGACY_VOL_THRESHOLDS["medium"]: return "medium"
    if atr_pct < LEGACY_VOL_THRESHOLDS["high"]: return "high"
    return "extreme"


def legacy_pick_range(price: float, atr_pct: float, tick_spacing: int = 60):
    """Reproduce legacy calc_optimal_range (ATR × regime, centered)."""
    regime = legacy_regime(atr_pct)
    mult = LEGACY_RANGE_MULT.get(regime, 0.8)
    half_width_pct = atr_pct * mult
    lo = price * (1 - half_width_pct / 100)
    hi = price * (1 + half_width_pct / 100)
    t_lo = (ce.price_usd_to_tick(lo) // tick_spacing) * tick_spacing
    t_hi = ((ce.price_usd_to_tick(hi) + tick_spacing - 1) // tick_spacing) * tick_spacing
    return t_lo, t_hi, regime, half_width_pct * 2


def atr_from_history(prices_1h: list, lookback: int = 14) -> float:
    """Approx ATR% from 1h price list (uses |Δp|/p as proxy for TR)."""
    window = prices_1h[-lookback - 1:]
    if len(window) < 2:
        return 5.0
    trs = [abs(window[i] - window[i - 1]) / window[i - 1] * 100
           for i in range(1, len(window))]
    return sum(trs) / len(trs)


def fetch_week():
    print("Fetching 7d data...")
    depth = json.loads(subprocess.check_output(
        ["onchainos", "defi", "depth-price-chart",
         "--investment-id", INVESTMENT_ID, "--chain", CHAIN, "--chart-type", "DEPTH"]))['data']
    depth = sorted([(p['tick'], int(p['liquidity'])) for p in depth], key=lambda x: x[0])

    prices = json.loads(subprocess.check_output(
        ["onchainos", "defi", "depth-price-chart",
         "--investment-id", INVESTMENT_ID, "--chain", CHAIN,
         "--chart-type", "PRICE", "--time-range", "WEEK"]))['data']
    prices = sorted([(p['timestamp'], float(p['token0Price'])) for p in prices
                     if float(p.get('token0Price', 0)) > 0], key=lambda x: x[0])

    rates = json.loads(subprocess.check_output(
        ["onchainos", "defi", "rate-chart",
         "--investment-id", INVESTMENT_ID, "--chain", CHAIN, "--time-range", "WEEK"]))['data']
    rates = sorted([(r['timestamp'], float(r['rate']), float(r['totalReward']))
                    for r in rates], key=lambda x: x[0])

    print(f"  depth: {len(depth)} ticks | prices: {len(prices)} 1h-points | rates: {len(rates)} 6h-points")
    return depth, prices, rates


def realized_fee_over_window(prices_next: list, fee_reward: float,
                             price_lo: float, price_hi: float,
                             share: float) -> float:
    """Approximate fee captured over a 6h window."""
    if not prices_next:
        return 0.0
    hits = sum(1 for _, p in prices_next if price_lo <= p <= price_hi)
    tir = hits / len(prices_next)
    return fee_reward * tir * share


def run_backtest():
    depth, prices, rates = fetch_week()
    if len(rates) < 4:
        print("Not enough rate points for backtest"); return

    # Index prices by hour for lookup
    price_by_hour = {p[0] // 3600000: p[1] for p in prices}

    rows = []
    cum_new = cum_legacy = 0.0
    print()
    print(f"{'time':<12} {'price':>8} {'atr%':>5} {'apy_3h':>7}  {'NEW range':>17} {'nW%':>5} {'nShare%':>7}  {'LEG range':>17} {'lW%':>5} {'lShare%':>7}  {'new_fee':>8} {'leg_fee':>8}")

    for i in range(4, len(rates) - 1):  # skip first 4 (need TIR history), last (no next window)
        ts, rate_t, total_reward = rates[i]
        hour_at = ts // 3600000
        price_t = price_by_hour.get(hour_at)
        if not price_t:
            # find closest hour
            price_t = min(prices, key=lambda p: abs(p[0] - ts))[1]

        # 24h price history ending at t
        hist_24h = [(p, pr) for p, pr in prices if ts - 24 * 3600000 <= p < ts]
        if len(hist_24h) < 12: continue
        atr_t = atr_from_history([pr for _, pr in hist_24h])

        # 3h APY proxy: this 6h rate (we don't have finer)
        apy_3h = rate_t

        # Gate check
        if apy_3h < ce.MIN_APY:
            new_range = None
        else:
            new_range = ce.find_best_range(
                price_t, atr_t, CAPITAL_USD,
                depth, hist_24h, rates[max(0, i - 2):i + 1],
                min_apy=ce.MIN_APY,
            )

        leg_tlo, leg_thi, leg_regime, leg_w = legacy_pick_range(price_t, atr_t)
        leg_rs = ce.score_range(leg_tlo, leg_thi, CAPITAL_USD, price_t, depth, hist_24h, apy_3h)

        # Next 6h prices
        next_prices = [(p, pr) for p, pr in prices if ts <= p < ts + 6 * 3600000]

        new_fee = realized_fee_over_window(
            next_prices, total_reward,
            new_range.price_lo_usd if new_range else 0,
            new_range.price_hi_usd if new_range else 0,
            new_range.share if new_range else 0,
        )
        leg_fee = realized_fee_over_window(
            next_prices, total_reward,
            leg_rs.price_lo_usd, leg_rs.price_hi_usd, leg_rs.share,
        )

        cum_new += new_fee
        cum_legacy += leg_fee

        import datetime
        tstr = datetime.datetime.fromtimestamp(ts / 1000, tz=datetime.timezone.utc).strftime("%m-%d %H:%M")

        if new_range:
            new_str = f"${new_range.price_lo_usd:.0f}-${new_range.price_hi_usd:.0f}"
            new_w = new_range.width_pct
            new_sh = new_range.share * 100
        else:
            new_str, new_w, new_sh = "-- gated --", 0, 0

        print(f"{tstr:<12} ${price_t:>7.0f} {atr_t:>4.2f}% {apy_3h*100:>5.0f}%  {new_str:>17} {new_w:>4.2f}% {new_sh:>6.4f}%  ${leg_rs.price_lo_usd:.0f}-${leg_rs.price_hi_usd:.0f} {leg_w:>4.2f}% {leg_rs.share*100:>6.4f}%  ${new_fee:>7.2f} ${leg_fee:>7.2f}")

        rows.append({"ts": ts, "new_fee": new_fee, "leg_fee": leg_fee,
                     "atr": atr_t, "apy": apy_3h})

    print()
    print(f"Cumulative realized fee (on ${CAPITAL_USD}):")
    print(f"  NEW (CE optimizer):  ${cum_new:>8.2f}")
    print(f"  LEGACY (ATR×regime): ${cum_legacy:>8.2f}")
    uplift = cum_new / cum_legacy if cum_legacy > 0 else float('inf')
    print(f"  Uplift:              {uplift:.2f}×")
    # Annualized
    n_windows = len(rows)
    days = n_windows * 6 / 24
    print(f"  Window: {days:.1f}d over {n_windows} evaluations")
    if days > 0:
        new_apr = cum_new / CAPITAL_USD * 365 / days * 100
        leg_apr = cum_legacy / CAPITAL_USD * 365 / days * 100
        print(f"  Annualized APR (NEW):    {new_apr:.1f}%")
        print(f"  Annualized APR (LEGACY): {leg_apr:.1f}%")


if __name__ == "__main__":
    run_backtest()
