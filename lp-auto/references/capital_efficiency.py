"""Capital-efficiency range optimizer for V3 LP.

Replaces the dynamic_width (σ/w)² model with a data-driven scorer that uses
three live signals from onchainos:
  1. Pool depth chart  — active liquidity L(tick) snapshot
  2. Pool price chart  — 24h hourly ETH price history (TIR estimation)
  3. Pool rate chart   — 24h hourly APY / totalReward (activity gate)

Scoring: `share × TIR × recent_3h_apy`, gated by `recent_3h_apy >= MIN_APY`.

All liquidity math uses V3 core formulas (Uniswap whitepaper §6.2); no RPC
calls per candidate — one onchainos call per data source, cached per tick.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

# ── Config defaults (overridable via cl_lp config.json "capital_efficiency") ──
MIN_APY = 0.10                              # pool gate: 10% annualized
WIDTH_MULTIPLIERS = [0.1, 0.3, 0.8, 1.2, 1.6]
CENTER_OFFSETS_PCT = [-20, -10, 0, 10, 20]
GATE_WINDOW_HOURS = 3
TIR_WINDOW_HOURS = 24
CACHE_TTL_SECONDS = 300                     # 5 min; tick runs every ~5 min
REBALANCE_COST_USD = 5.0                    # gas + 2% swap slippage on ~$200

# Pool-specific (WETH/USDC on Base, token0=WETH 18d, token1=USDC 6d)
TOKEN0_DECIMALS = 18
TOKEN1_DECIMALS = 6


# ── onchainos helpers ────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, dict]] = {}


def _run_onchainos(args: list[str]) -> dict:
    """Invoke onchainos and parse JSON response; raises on failure."""
    proc = subprocess.run(
        ["onchainos", *args], capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise RuntimeError(f"onchainos failed: {proc.stderr[:200]}")
    return json.loads(proc.stdout)


def _cached(key: str, fn):
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_TTL_SECONDS:
        return _cache[key][1]
    val = fn()
    _cache[key] = (now, val)
    return val


def fetch_depth(investment_id: str, chain: str = "base") -> list[tuple[int, int]]:
    """Return [(tick, L_int)] sorted ascending by tick."""
    def _fetch():
        r = _run_onchainos([
            "defi", "depth-price-chart",
            "--investment-id", investment_id,
            "--chain", chain,
            "--chart-type", "DEPTH",
        ])
        return sorted(
            [(p["tick"], int(p["liquidity"])) for p in r["data"]],
            key=lambda x: x[0],
        )
    return _cached(f"depth:{investment_id}:{chain}", _fetch)


def fetch_hourly_prices(investment_id: str, chain: str = "base") -> list[tuple[int, float]]:
    """Return [(ts_ms, eth_usd)] for last 24h, sorted ascending."""
    def _fetch():
        r = _run_onchainos([
            "defi", "depth-price-chart",
            "--investment-id", investment_id,
            "--chain", chain,
            "--chart-type", "PRICE",
            "--time-range", "DAY",
        ])
        return sorted(
            [(p["timestamp"], float(p["token0Price"])) for p in r["data"]
             if float(p.get("token0Price", 0)) > 0],
            key=lambda x: x[0],
        )
    return _cached(f"price:{investment_id}:{chain}", _fetch)


def fetch_hourly_rates(investment_id: str, chain: str = "base") -> list[tuple[int, float, float]]:
    """Return [(ts_ms, rate_apy, total_reward_usd)] for last 24h, sorted ascending."""
    def _fetch():
        r = _run_onchainos([
            "defi", "rate-chart",
            "--investment-id", investment_id,
            "--chain", chain,
            "--time-range", "DAY",
        ])
        return sorted(
            [(p["timestamp"], float(p["rate"]), float(p["totalReward"]))
             for p in r["data"]],
            key=lambda x: x[0],
        )
    return _cached(f"rate:{investment_id}:{chain}", _fetch)


# ── Price/tick conversions (WETH/USDC, token0=WETH token1=USDC) ──────────────

def price_usd_to_tick(price_usd: float) -> int:
    """ETH price in USD → pool tick index. raw = price_usd × 10^(d1-d0)."""
    raw = price_usd * (10 ** (TOKEN1_DECIMALS - TOKEN0_DECIMALS))
    return int(math.log(raw) / math.log(1.0001))


def tick_to_price_usd(tick: int) -> float:
    """Pool tick → ETH price in USD."""
    raw = 1.0001 ** tick
    return raw * (10 ** (TOKEN0_DECIMALS - TOKEN1_DECIMALS))


def _sqrt_raw(price_usd: float) -> float:
    """sqrt of raw pool price (raw = token1_units / token0_units)."""
    raw = price_usd * (10 ** (TOKEN1_DECIMALS - TOKEN0_DECIMALS))
    return math.sqrt(raw)


# ── V3 liquidity math ────────────────────────────────────────────────────────

def calc_my_L(capital_usd: float, price_usd: float,
              price_lo_usd: float, price_hi_usd: float) -> float:
    """V3 L from USD capital, assuming current price ∈ [lo, hi].

    Per Uniswap V3 whitepaper §6.2:
      Δx = L × (√Pb - √P) / (√Pb × √P)      [token0, here WETH wei]
      Δy = L × (√P - √Pa)                    [token1, here USDC µunits]
    """
    sqrt_p = _sqrt_raw(price_usd)
    sqrt_a = _sqrt_raw(price_lo_usd)
    sqrt_b = _sqrt_raw(price_hi_usd)

    if sqrt_p <= sqrt_a:
        # All capital in token0 (WETH)
        weth_wei = capital_usd / price_usd * (10 ** TOKEN0_DECIMALS)
        return weth_wei * sqrt_a * sqrt_b / (sqrt_b - sqrt_a)
    if sqrt_p >= sqrt_b:
        # All capital in token1 (USDC)
        usdc_units = capital_usd * (10 ** TOKEN1_DECIMALS)
        return usdc_units / (sqrt_b - sqrt_a)

    # In range: capital split by V3 ratio
    # Δx_usd = L × (√Pb - √P) / (√Pb × √P) × price_usd / 10^d0
    # Δy_usd = L × (√P - √Pa) / 10^d1
    x_usd_per_L = (sqrt_b - sqrt_p) / (sqrt_b * sqrt_p) * price_usd / (10 ** TOKEN0_DECIMALS)
    y_usd_per_L = (sqrt_p - sqrt_a) / (10 ** TOKEN1_DECIMALS)
    usd_per_L = x_usd_per_L + y_usd_per_L
    return capital_usd / usd_per_L


def avg_L_in_range(depth: list[tuple[int, int]], tick_lo: int, tick_hi: int) -> float:
    """Average active liquidity across ticks in [tick_lo, tick_hi].

    Depth is sparse (only tick points where L changes). We interpolate: L is
    constant between adjacent sample points. Return tick-weighted mean.
    """
    if tick_lo >= tick_hi or not depth:
        return 0.0

    samples = [(t, L) for t, L in depth if tick_lo <= t <= tick_hi]
    # Bracket: find surrounding samples to handle range edges
    below = [(t, L) for t, L in depth if t < tick_lo]
    above = [(t, L) for t, L in depth if t > tick_hi]
    if below:
        samples.insert(0, (tick_lo, below[-1][1]))
    if above:
        samples.append((tick_hi, above[0][1]))
    if not samples:
        return 0.0
    if len(samples) == 1:
        return float(samples[0][1])

    total_L_weight = 0.0
    total_ticks = 0
    for i in range(len(samples) - 1):
        span = samples[i + 1][0] - samples[i][0]
        if span <= 0:
            continue
        total_L_weight += samples[i][1] * span
        total_ticks += span
    return total_L_weight / total_ticks if total_ticks else 0.0


# ── TIR + APY signals ────────────────────────────────────────────────────────

def time_in_range(prices: list[tuple[int, float]],
                  price_lo_usd: float, price_hi_usd: float) -> float:
    """Fraction of hourly samples where price ∈ [lo, hi]."""
    if not prices:
        return 0.0
    hits = sum(1 for _, p in prices if price_lo_usd <= p <= price_hi_usd)
    return hits / len(prices)


def recent_apy(rates: list[tuple[int, float, float]], window_hours: int = 3) -> float:
    """Mean APY (as decimal, e.g. 0.64 = 64%) over the last `window_hours`."""
    if not rates:
        return 0.0
    recent = rates[-window_hours:]
    if not recent:
        return 0.0
    return sum(r for _, r, _ in recent) / len(recent)


def expected_rebalances_24h(prices: list[tuple[int, float]],
                             price_lo: float, price_hi: float) -> int:
    """Estimate rebalance events from 24h price path length / band width.

    Path-length method: total |Δprice| over 24h, divided by band width.
    Roughly 1 traversal = 1 out-of-range event requiring rebalance.
    More robust than counting boundary crossings (which misses trending paths).
    """
    if len(prices) < 2 or price_hi <= price_lo:
        return 999
    width = price_hi - price_lo
    path = sum(abs(prices[i][1] - prices[i-1][1])
               for i in range(1, len(prices)))
    return max(0, int(path / width))


# ── Scoring + optimization ───────────────────────────────────────────────────

@dataclass
class RangeScore:
    tick_lo: int
    tick_hi: int
    price_lo_usd: float
    price_hi_usd: float
    my_L: float
    avg_L: float
    share: float
    tir: float
    apy_3h: float
    expected_fee_24h: float
    expected_rebalances: int
    rebalance_cost: float
    net_24h: float              # fee - cost (THE score)
    score: float                # == net_24h
    width_pct: float
    offset_pct: int
    details: dict = field(default_factory=dict)


def score_range(
    tick_lo: int, tick_hi: int,
    capital_usd: float, current_price_usd: float,
    depth: list[tuple[int, int]],
    prices: list[tuple[int, float]],
    rates: list[tuple[int, float, float]],
    apy_3h: float,
    rebalance_cost: float = REBALANCE_COST_USD,
) -> RangeScore:
    price_lo = tick_to_price_usd(tick_lo)
    price_hi = tick_to_price_usd(tick_hi)
    my_L = calc_my_L(capital_usd, current_price_usd, price_lo, price_hi)
    a_L = avg_L_in_range(depth, tick_lo, tick_hi)
    share = my_L / (a_L + my_L) if (a_L + my_L) > 0 else 0.0
    tir = time_in_range(prices, price_lo, price_hi)

    # Expected 24h fee: sum(fee_h × 1{price_h ∈ range}) × share
    fee_captured_24h = sum(reward for ts, _, reward in rates
                            for tsp, p in prices
                            if ts // 3600000 == tsp // 3600000 and price_lo <= p <= price_hi)
    expected_fee = fee_captured_24h * share

    # Expected rebalance events (from 24h price path) × per-event cost
    n_rebals = expected_rebalances_24h(prices, price_lo, price_hi)
    cost_total = n_rebals * rebalance_cost

    net = expected_fee - cost_total

    return RangeScore(
        tick_lo=tick_lo, tick_hi=tick_hi,
        price_lo_usd=price_lo, price_hi_usd=price_hi,
        my_L=my_L, avg_L=a_L, share=share, tir=tir,
        apy_3h=apy_3h,
        expected_fee_24h=expected_fee,
        expected_rebalances=n_rebals,
        rebalance_cost=cost_total,
        net_24h=net,
        score=net,
        width_pct=(price_hi - price_lo) / current_price_usd * 100,
        offset_pct=0,
    )


def find_best_range(
    current_price_usd: float, atr_pct_1h: float, capital_usd: float,
    depth: list[tuple[int, int]],
    prices: list[tuple[int, float]],
    rates: list[tuple[int, float, float]],
    tick_spacing: int = 60,
    min_apy: float = MIN_APY,
    width_multipliers: list[float] = None,
    center_offsets_pct: list[int] = None,
) -> Optional[RangeScore]:
    """Return best-scoring RangeScore or None if pool is inactive.

    Gate: recent 3h APY < min_apy → None (pool dead, defer to legacy/skip).
    Constraint: range must cover current_price_usd.
    """
    apy = recent_apy(rates, GATE_WINDOW_HOURS)
    if apy < min_apy:
        return None

    ws = width_multipliers or WIDTH_MULTIPLIERS
    offs = center_offsets_pct or CENTER_OFFSETS_PCT
    candidates: list[RangeScore] = []

    for mult in ws:
        half_width_pct = max(atr_pct_1h * mult, 0.05)  # avoid zero
        for off in offs:
            center = current_price_usd * (1 + off / 100)
            lo = center * (1 - half_width_pct / 100)
            hi = center * (1 + half_width_pct / 100)
            # Hard constraint: must cover current price
            if not (lo <= current_price_usd <= hi):
                continue
            t_lo = (price_usd_to_tick(lo) // tick_spacing) * tick_spacing
            t_hi = ((price_usd_to_tick(hi) + tick_spacing - 1) // tick_spacing) * tick_spacing
            if t_hi <= t_lo:
                continue
            rs = score_range(t_lo, t_hi, capital_usd, current_price_usd,
                             depth, prices, rates, apy)
            rs.width_pct = half_width_pct * 2
            rs.offset_pct = off
            rs.details = {"width_mult": mult}
            candidates.append(rs)

    if not candidates:
        return None
    # Primary: max net 24h (fee - rebal cost). Tiebreak: smaller width.
    candidates.sort(key=lambda x: (-x.score, x.width_pct))
    best = candidates[0]
    # Final gate: net must be positive, else skip entry
    if best.net_24h <= 0:
        return None
    return best


# ── CLI for offline testing ──────────────────────────────────────────────────

def _fmt_range(rs: RangeScore) -> str:
    return (
        f"[${rs.price_lo_usd:.0f}-${rs.price_hi_usd:.0f}] "
        f"w={rs.width_pct:.2f}% off={rs.offset_pct:+d}% "
        f"share={rs.share*100:.4f}% TIR={rs.tir*100:.0f}% "
        f"fee=${rs.expected_fee_24h:.2f} rebal={rs.expected_rebalances}×"
        f" cost=${rs.rebalance_cost:.0f} net=${rs.net_24h:.2f}/24h"
    )


if __name__ == "__main__":
    import sys
    investment_id = sys.argv[1] if len(sys.argv) > 1 else "326890603"
    chain = sys.argv[2] if len(sys.argv) > 2 else "base"
    capital = float(sys.argv[3]) if len(sys.argv) > 3 else 454.0
    atr_1h = float(sys.argv[4]) if len(sys.argv) > 4 else 5.03

    print(f"Fetching live data for {investment_id} @ {chain}...")
    depth = fetch_depth(investment_id, chain)
    prices = fetch_hourly_prices(investment_id, chain)
    rates = fetch_hourly_rates(investment_id, chain)
    current = prices[-1][1] if prices else 0
    print(f"  depth: {len(depth)} tick points")
    print(f"  prices: {len(prices)} hourly, current=${current:.2f}")
    print(f"  rates: {len(rates)} hourly, apy_3h={recent_apy(rates)*100:.1f}%")

    best = find_best_range(current, atr_1h, capital, depth, prices, rates)
    if best is None:
        print("Pool gate: inactive (apy_3h < 10%) → None")
    else:
        print()
        print(f"Best: {_fmt_range(best)}")
        print(f"  my_L={best.my_L:.3e}  avg_L_in_range={best.avg_L:.3e}")
