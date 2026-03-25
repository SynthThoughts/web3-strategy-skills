#!/usr/bin/env python3
"""
ETH/USDC Dynamic Grid Trading Bot v1.0 — Base Chain
Improvements over v3:
  1. Multi-timeframe analysis (5min tick + 1h trend + 4h structure)
  2. Trend-adaptive grid: asymmetric sizing in trending markets
  3. K-line/OHLC volatility via onchainos market kline
  4. Improved sell logic with trailing grid profit locking
  5. HODL-alpha tracking with trend-follow overlay

Uses OKX DEX API (via onchainos CLI) + OnchainOS Agentic Wallet (TEE signing).
Designed for OpenClaw cron integration.
"""

import bisect
import json
import subprocess
import os
import sys
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Load .env if present ────────────────────────────────────────────────────


def _load_env():
    # Check script dir first, then parent (skill root for openclaw installs)
    for base in [Path(__file__).parent, Path(__file__).parent.parent]:
        env_file = base / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            return


_load_env()

# ── Config ──────────────────────────────────────────────────────────────────

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

# Token addresses (Base chain)
ETH_ADDR = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
USDC_ADDR = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CHAIN_ID = "8453"


def _resolve_wallet_addr() -> str:
    """Resolve wallet address: env override > onchainos wallet addresses."""
    env_addr = os.environ.get("WALLET_ADDR", "")
    if env_addr:
        return env_addr
    try:
        result = subprocess.run(
            ["onchainos", "wallet", "addresses"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            if data.get("ok") and data.get("data", {}).get("evm"):
                evm_addrs = data["data"]["evm"]
                # Prefer the address matching CHAIN_ID, fallback to first EVM
                for entry in evm_addrs:
                    if entry.get("chainIndex") == CHAIN_ID:
                        return entry["address"]
                return evm_addrs[0]["address"]
    except Exception:
        pass
    return ""


# Switch onchainos to the correct account if specified
_account_id = os.environ.get("ONCHAINOS_ACCOUNT_ID", "")
if _account_id:
    try:
        _sw = subprocess.run(
            ["onchainos", "wallet", "switch", _account_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if _sw.returncode != 0:
            print(
                f"WARN: onchainos account switch failed: {_sw.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"WARN: onchainos account switch error: {e}", file=sys.stderr)

WALLET_ADDR = _resolve_wallet_addr()
if not WALLET_ADDR:
    print(
        "ERROR: No wallet address found. Login with `onchainos wallet login` or set WALLET_ADDR env.",
        file=sys.stderr,
    )

# ── Grid Parameters ─────────────────────────────────────────────

GRID_LEVELS = 6  # 6 levels
GRID_TYPE = "arithmetic"  # "arithmetic" | "geometric"
MAX_TRADE_PCT = 0.12  # max 12% of total portfolio per trade
MIN_TRADE_USD = 5.0  # minimum trade size in USD
GAS_RESERVE_ETH = 0.003  # Reserve for gas (Base L2 gas is <$0.01)
SLIPPAGE_PCT = 1  # 1% slippage for DEX aggregator swaps
EMA_PERIOD = 20  # periods for EMA center (applied to 1H kline = 20h)

# Trend-adaptive volatility multiplier (directional)
VOLATILITY_MULTIPLIER_BASE = 1.5  # base multiplier (neutral/weak trend)
VOLATILITY_MULTIPLIER_BULL = 3.0  # bullish: wider grid → hold position, trade less
VOLATILITY_MULTIPLIER_BEAR = 1.0  # bearish: tighter grid → exit faster, trade more
# Asymmetric grid — different step sizes for buy vs sell side
# Bullish: tighter buy (accumulate fast) + wider sell (hold longer)
# Bearish: tighter sell (exit fast) + wider buy (wait for dip)
ASYM_FACTOR = 0.4  # max asymmetry ratio (0 = symmetric, 1 = fully asymmetric)

# Sizing strategy (trend-adaptive)
SIZING_STRATEGY = "trend_adaptive"  # "equal" | "martingale" | "anti_martingale" | "pyramid" | "trend_adaptive"
SIZING_MULTIPLIER_MIN = 0.5
SIZING_MULTIPLIER_MAX = 2.0

# Stop-loss / take-profit protection
STOP_LOSS_PCT = 0.15  # stop at 15% loss from cost basis
TRAILING_STOP_PCT = 0.10  # stop at 10% drawdown from peak

# Auto-resume after stop-loss
STOP_AUTO_RESUME = True  # enable automatic recovery after stop
STOP_COOLDOWN_MINUTES = 60  # minimum wait time after stop before considering resume
STOP_RESUME_BOUNCE_PCT = 0.01  # price must recover 1% from stop price
STOP_RESUME_MAX_BEARISH = (
    0.5  # resume only if trend strength < this (not strongly bearish)
)


# Position limits (trend-asymmetric)
POSITION_MAX_PCT_DEFAULT = 80  # Block BUY when ETH > this %
POSITION_MIN_PCT_DEFAULT = 20  # Block SELL when ETH < this %
POSITION_MAX_PCT_BULLISH = 90  # Allow more ETH in bullish trend
POSITION_MIN_PCT_BEARISH = 15  # Allow less ETH in bearish trend

# Adaptive step bounds (as fraction of price)
STEP_MIN_PCT = 0.010  # 1.0% (covers DEX costs)
STEP_MAX_PCT = 0.060  # cap: 6%
VOL_RECALIBRATE_RATIO = 0.3  # recalibrate if vol changes >30% from last grid
MAX_CONSECUTIVE_ERRORS = 5  # circuit breaker threshold
MAX_SAME_DIR_TRADES = 3  # max consecutive same-direction trades before pause
COOLDOWN_AFTER_ERRORS = 3600  # 1 hour cooldown after circuit break
QUIET_INTERVAL = 3600  # seconds between no-trade status reports (1 hour)
MIN_TRADE_INTERVAL = 1800  # 30min cooldown between same-direction trades
GRID_RECALIBRATE_HOURS = 12  # Keep grid fixed; recalibrate only when needed
UPSIDE_CONFIRM_TICKS = (
    6  # 30min: price must hold above grid before upside recalibration
)
MAX_CENTER_SHIFT_PCT = 0.03  # max 3% grid center shift per recalibration (anti-chase)

# Multi-timeframe settings
MTF_SHORT_PERIOD = 5  # 5-bar EMA (25min @ 5min tick)
MTF_MEDIUM_PERIOD = 12  # 12-bar EMA (1h @ 5min tick)
MTF_LONG_PERIOD = 48  # 48-bar EMA (4h @ 5min tick)
MTF_STRUCTURE_PERIOD = 96  # 96-bar (8h @ 5min tick) for structure detection

# Sell improvement — trailing grid lock
SELL_TRAIL_TICKS = 0  # sell immediately when price hits level
SELL_MOMENTUM_THRESHOLD = 0.005  # skip sell if 1h momentum > 0.5% (strong uptrend)

# Dip-buy accumulation (buy-only mode when sell is blocked)
DIP_BUY_LOOKBACK = 12  # 1 hour of 5min bars for recent-high detection
DIP_BUY_MIN_DRAWDOWN = 0.005  # 0.5% minimum pullback from recent high
DIP_BUY_COOLDOWN = 1800  # 30min cooldown between dip buys
DIP_BUY_TIERS = [  # (drawdown_threshold, sizing_multiplier)
    (0.03, 2.0),  # ≥3% drop: 200% size
    (0.02, 1.5),  # ≥2% drop: 150% size
    (0.01, 1.0),  # ≥1% drop: 100% size
    (0.005, 0.5),  # ≥0.5% drop: 50% size
]
DIP_BUY_MOMENTUM_FLOOR = -0.5  # skip if 1h momentum < -0.5% (still falling hard)
DIP_BUY_REVERSAL_TICKS = (
    2  # price must rise for N consecutive ticks to confirm reversal
)

# Paths
SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "grid_state_v1.json"
LOG_FILE = SCRIPT_DIR / "grid_bot_v1.log"
MAX_LOG_BYTES = 1_000_000  # 1MB log rotation

# ── Logging ─────────────────────────────────────────────────────────────────


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            lines = LOG_FILE.read_text().splitlines()
            LOG_FILE.write_text("\n".join(lines[len(lines) // 2 :]) + "\n")
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── onchainos CLI wrapper ────────────────────────────────────────────────────


def onchainos_cmd(args: list[str], timeout: int = 30) -> dict | None:
    """Run onchainos CLI command, return parsed JSON."""
    env = os.environ.copy()
    env.setdefault("OKX_API_KEY", OKX_API_KEY)
    env.setdefault("OKX_SECRET_KEY", OKX_SECRET)
    env.setdefault("OKX_PASSPHRASE", OKX_PASSPHRASE)
    try:
        result = subprocess.run(
            ["onchainos"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = result.stdout.strip() if result.stdout else ""
        if output:
            try:
                data = json.loads(output)
                if isinstance(data, dict) and "ok" in data:
                    return data
                return {"ok": True, "data": data if isinstance(data, list) else [data]}
            except json.JSONDecodeError:
                pass
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            log(
                f"onchainos rc={result.returncode}: {' '.join(args[:3])} "
                f"{'stderr=' + stderr[:150] if stderr else 'no output'}"
            )
    except subprocess.TimeoutExpired:
        log(f"onchainos timeout: {' '.join(args[:3])}")
    except Exception as e:
        log(f"onchainos error: {e}")
    return None


# ── Price & Balance ─────────────────────────────────────────────────────────


def get_eth_price() -> float | None:
    """Get ETH/USDC price via onchainos swap quote."""
    data = onchainos_cmd(
        [
            "swap",
            "quote",
            "--from",
            ETH_ADDR,
            "--to",
            USDC_ADDR,
            "--amount",
            "1000000000000000000",
            "--chain",
            "base",
        ]
    )
    if data and data.get("ok") and data.get("data"):
        return int(data["data"][0]["toTokenAmount"]) / 1e6
    return None


def get_balances() -> tuple[float, float] | None:
    """Get ETH and USDC balances. Returns None on query failure."""
    data = onchainos_cmd(["wallet", "balance", "--chain", CHAIN_ID], timeout=15)
    if not data or not data.get("ok") or not data.get("data"):
        log(f"Balance query failed, raw: {json.dumps(data)[:200] if data else 'None'}")
        return None
    eth, usdc = 0.0, 0.0
    details = data["data"].get("details", [])
    for chain_detail in details:
        for token in chain_detail.get("tokenAssets", []):
            if token.get("tokenAddress") == "" and token.get("symbol") == "ETH":
                eth = float(token.get("balance", "0"))
            elif token.get("tokenAddress", "").lower() == USDC_ADDR.lower():
                usdc = float(token.get("balance", "0"))
    return eth, usdc


# ── K-line / OHLC Data ────────────────────────────────────────────────


def get_kline_data(bar: str = "1H", limit: int = 24) -> list[dict] | None:
    """Fetch K-line data via onchainos market kline.
    Returns list of {open, high, low, close, volume, ts}."""
    data = onchainos_cmd(
        [
            "market",
            "kline",
            "--address",
            ETH_ADDR,
            "--chain",
            "base",
            "--bar",
            bar,
            "--limit",
            str(limit),
        ],
        timeout=15,
    )
    if data and data.get("ok") and data.get("data"):
        candles = []
        for c in data["data"]:
            try:
                # onchainos returns arrays: [ts, open, high, low, close, volume, ...]
                if isinstance(c, list) and len(c) >= 6:
                    candles.append(
                        {
                            "ts": int(c[0]),
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                            "volume": float(c[5]),
                        }
                    )
                elif isinstance(c, dict):
                    candles.append(
                        {
                            "ts": int(c.get("ts", 0)),
                            "open": float(c.get("o", 0) or c.get("open", 0)),
                            "high": float(c.get("h", 0) or c.get("high", 0)),
                            "low": float(c.get("l", 0) or c.get("low", 0)),
                            "close": float(c.get("c", 0) or c.get("close", 0)),
                            "volume": float(c.get("vol", 0) or c.get("volume", 0)),
                        }
                    )
            except (ValueError, TypeError, IndexError):
                continue
        return candles if candles else None
    return None


def calc_kline_volatility(candles: list[dict]) -> float:
    """Calculate true range based volatility from OHLC candles.
    Returns average true range as percentage of price."""
    if not candles or len(candles) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, len(candles)):
        hi = candles[i]["high"]
        lo = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
        true_ranges.append(tr)
    atr = sum(true_ranges) / len(true_ranges)
    avg_price = sum(c["close"] for c in candles) / len(candles)
    return (atr / avg_price) * 100 if avg_price > 0 else 0.0


# ── Multi-Timeframe Analysis ────────────────────────────────────────────


def calc_ema(prices: list[float], period: int) -> float:
    """Calculate Exponential Moving Average."""
    if not prices:
        return 0.0
    if len(prices) < period:
        return sum(prices) / len(prices)
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_volatility(prices: list[float]) -> float:
    """Calculate standard deviation of prices."""
    if len(prices) < 2:
        return 0.0
    mean = sum(prices) / len(prices)
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    return math.sqrt(variance)


def analyze_multi_timeframe(history: list[float], price: float) -> dict:
    """Multi-timeframe trend analysis.
    Returns {trend: str, strength: float, momentum_1h: float, structure: str,
             ema_short, ema_medium, ema_long}."""
    result = {
        "trend": "neutral",
        "strength": 0.0,
        "momentum_1h": 0.0,
        "momentum_4h": 0.0,
        "structure": "ranging",
        "ema_short": price,
        "ema_medium": price,
        "ema_long": price,
    }

    if len(history) < MTF_SHORT_PERIOD:
        return result

    ema_short = calc_ema(history, min(MTF_SHORT_PERIOD, len(history)))
    ema_medium = calc_ema(history, min(MTF_MEDIUM_PERIOD, len(history)))
    ema_long = calc_ema(history, min(MTF_LONG_PERIOD, len(history)))
    result["ema_short"] = round(ema_short, 2)
    result["ema_medium"] = round(ema_medium, 2)
    result["ema_long"] = round(ema_long, 2)

    # 1h momentum
    if len(history) >= 12:
        result["momentum_1h"] = round((price - history[-12]) / history[-12] * 100, 3)

    # 4h momentum
    if len(history) >= 48:
        result["momentum_4h"] = round((price - history[-48]) / history[-48] * 100, 3)

    # Trend detection: EMA alignment
    if ema_short > ema_medium > ema_long:
        result["trend"] = "bullish"
        # Strength: how spread apart the EMAs are
        spread = (ema_short - ema_long) / ema_long * 100
        result["strength"] = round(min(spread / 2.0, 1.0), 3)  # normalize, cap at 1.0
    elif ema_short < ema_medium < ema_long:
        result["trend"] = "bearish"
        spread = (ema_long - ema_short) / ema_long * 100
        result["strength"] = round(min(spread / 2.0, 1.0), 3)
    else:
        result["trend"] = "neutral"
        result["strength"] = 0.0

    # Structure detection: higher highs/lows vs lower highs/lows
    if len(history) >= MTF_STRUCTURE_PERIOD:
        # Split into 4 segments, check if pivot highs/lows are ascending
        seg_len = MTF_STRUCTURE_PERIOD // 4
        segments = [
            history[-(i + 1) * seg_len : -i * seg_len or None] for i in range(3, -1, -1)
        ]
        seg_highs = [max(s) for s in segments if s]
        seg_lows = [min(s) for s in segments if s]

        hh = all(seg_highs[i] >= seg_highs[i - 1] for i in range(1, len(seg_highs)))
        hl = all(seg_lows[i] >= seg_lows[i - 1] for i in range(1, len(seg_lows)))
        lh = all(seg_highs[i] <= seg_highs[i - 1] for i in range(1, len(seg_highs)))
        ll = all(seg_lows[i] <= seg_lows[i - 1] for i in range(1, len(seg_lows)))

        if hh and hl:
            result["structure"] = "uptrend"
        elif lh and ll:
            result["structure"] = "downtrend"
        else:
            result["structure"] = "ranging"

    return result


# ── Trend-Adaptive Grid Calculation ─────────────────────────────────────


def _build_level_prices(
    center: float,
    buy_step: float,
    sell_step: float,
    half: int,
    grid_type: str = "arithmetic",
) -> list[float]:
    """Build asymmetric level_prices: below center uses buy_step, above uses sell_step."""
    if grid_type == "geometric" and center > 0:
        buy_ratio = 1 + (buy_step / center)
        sell_ratio = 1 + (sell_step / center)
        below = [round(center / (buy_ratio ** (half - i)), 2) for i in range(half)]
        above = [round(center * (sell_ratio ** (i + 1)), 2) for i in range(half)]
        return below + [round(center, 2)] + above
    else:
        below = [round(center - (half - i) * buy_step, 2) for i in range(half)]
        above = [round(center + (i + 1) * sell_step, 2) for i in range(half)]
        return below + [round(center, 2)] + above


def calc_dynamic_grid(
    current_price: float, price_history: list[float], mtf: dict | None = None
) -> dict:
    """Calculate dynamic grid with trend-adaptive parameters.
    Uses 1H kline for grid center (more robust than 5min tick history).
    In trending markets, use wider grid (hold more, trade less).
    """
    # Prefer 1H kline for center price — more stable than 5min ticks
    hourly_closes: list[float] = []
    candles = get_kline_data(bar="1H", limit=max(EMA_PERIOD, 24))
    if candles and len(candles) >= 5:
        hourly_closes = [c["close"] for c in candles]

    if hourly_closes:
        center = calc_ema(hourly_closes, min(EMA_PERIOD, len(hourly_closes)))
        log(
            f"Grid center: EMA({min(EMA_PERIOD, len(hourly_closes))}) on 1H kline = ${center:.2f}"
        )
    elif len(price_history) >= 5:
        center = calc_ema(price_history, min(EMA_PERIOD, len(price_history)))
        log(
            f"Grid center: EMA({min(EMA_PERIOD, len(price_history))}) on 5min fallback = ${center:.2f}"
        )
    else:
        center = current_price
        vol_pct = 0.0
        step = current_price * 0.01
        buy_step = step
        sell_step = step
        log(f"Grid center: cold start, using current price ${center:.2f}")

    # Calculate volatility and step size (skip if cold start already set step)
    if candles and len(candles) >= 2:
        # Use 1H ATR for step sizing (more robust than stddev)
        vol_pct = calc_kline_volatility(candles)  # ATR as % of price
        atr_dollar = vol_pct / 100 * current_price
        log(f"ATR(1H, {len(candles)} bars): {vol_pct:.2f}% = ${atr_dollar:.1f}")

        # Trend-adaptive multiplier — directional: bull widens, bear tightens
        vol_mult = VOLATILITY_MULTIPLIER_BASE
        strength = mtf.get("strength", 0) if mtf else 0
        trend = mtf.get("trend", "neutral") if mtf else "neutral"
        if strength > 0.3:
            if trend == "bullish":
                vol_mult = (
                    VOLATILITY_MULTIPLIER_BASE
                    + (VOLATILITY_MULTIPLIER_BULL - VOLATILITY_MULTIPLIER_BASE)
                    * strength
                )  # 2.0 → 3.0: widen to hold
            elif trend == "bearish":
                vol_mult = (
                    VOLATILITY_MULTIPLIER_BASE
                    - (VOLATILITY_MULTIPLIER_BASE - VOLATILITY_MULTIPLIER_BEAR)
                    * strength
                )  # 2.0 → 1.5: tighten to exit

        # Asymmetric buy/sell multipliers based on trend direction
        # Bullish: tighter buy (accumulate fast) + wider sell (hold longer)
        # Bearish: tighter sell (exit fast) + wider buy (wait for dip)
        asym = ASYM_FACTOR * strength if strength > 0.3 else 0
        if trend == "bullish":
            buy_mult = vol_mult * (1 - asym)
            sell_mult = vol_mult * (1 + asym)
        elif trend == "bearish":
            buy_mult = vol_mult * (1 + asym)
            sell_mult = vol_mult * (1 - asym)
        else:
            buy_mult = vol_mult
            sell_mult = vol_mult

        half_levels = GRID_LEVELS / 2
        buy_step = (buy_mult * atr_dollar) / half_levels
        sell_step = (sell_mult * atr_dollar) / half_levels
        step_floor = current_price * STEP_MIN_PCT
        step_ceil = current_price * STEP_MAX_PCT
        buy_step = max(step_floor, min(step_ceil, buy_step))
        sell_step = max(step_floor, min(step_ceil, sell_step))
        step = (buy_step + sell_step) / 2  # backward-compatible average

        if asym > 0:
            log(
                f"Asymmetric grid ({trend}): buy_step=${buy_step:.1f} "
                f"sell_step=${sell_step:.1f} (asym={asym:.2f}, mult={vol_mult:.2f})"
            )
        else:
            log(
                f"Step: ${step:.1f} ({step / current_price * 100:.2f}% of price, "
                f"ATR=${atr_dollar:.1f}, mult={vol_mult:.2f})"
            )
    elif len(price_history) >= 5:
        # Fallback: stddev from 5min ticks (symmetric only)
        volatility = calc_volatility(price_history)
        avg_price = sum(price_history) / len(price_history)
        vol_pct = (volatility / avg_price) * 100 if avg_price > 0 else 0
        vol_mult = VOLATILITY_MULTIPLIER_BASE
        step = (vol_mult * volatility) / (GRID_LEVELS / 2)
        step_floor = current_price * STEP_MIN_PCT
        step_ceil = current_price * STEP_MAX_PCT
        step = max(step_floor, min(step_ceil, step))
        buy_step = step
        sell_step = step
        log(f"Step (5min fallback): ${step:.1f} ({vol_pct:.2f}% stddev)")

    # Hard floor: at least $5
    buy_step = max(buy_step, 5.0)
    sell_step = max(sell_step, 5.0)
    step = (buy_step + sell_step) / 2

    # Build asymmetric level_prices: below center uses buy_step, above uses sell_step
    half = int(GRID_LEVELS / 2)
    level_prices = _build_level_prices(center, buy_step, sell_step, half, GRID_TYPE)
    low = level_prices[0]
    high = level_prices[-1]

    return {
        "center": round(center, 2),
        "step": round(step, 2),
        "buy_step": round(buy_step, 2),
        "sell_step": round(sell_step, 2),
        "levels": GRID_LEVELS,
        "range": [round(low, 2), round(high, 2)],
        "vol_pct": round(vol_pct, 2),
        "type": GRID_TYPE,
        "level_prices": level_prices,
    }


def price_to_level(price: float, grid: dict) -> int:
    """Convert price to grid level (0 = bottom, GRID_LEVELS = top)."""
    level_prices = grid.get("level_prices")
    if level_prices:
        level = bisect.bisect_right(level_prices, price) - 1
        return max(0, min(GRID_LEVELS, level))
    low = grid["range"][0]
    step = grid["step"]
    if step <= 0:
        return GRID_LEVELS // 2
    level = int((price - low) / step)
    return max(0, min(GRID_LEVELS, level))


# ── Trade Execution ─────────────────────────────────────────────────────────


def _calc_sizing_multiplier(
    level: int,
    grid_levels: int,
    direction: str,
    mtf: dict | None = None,
) -> float:
    """Trend-adaptive sizing.
    - In bullish trend: buy more (larger buys), sell less (smaller sells) → hold more ETH
    - In bearish trend: buy less, sell more → hold more USDC
    """
    base_mult = 1.0

    if SIZING_STRATEGY == "trend_adaptive" and mtf:
        trend = mtf.get("trend", "neutral")
        strength = mtf.get("strength", 0)

        if trend == "bullish":
            if direction == "BUY":
                # In uptrend, buy aggressively
                base_mult = 1.0 + strength * (SIZING_MULTIPLIER_MAX - 1.0)
            else:
                # In uptrend, sell conservatively (hold more ETH to capture upside)
                base_mult = 1.0 - strength * (1.0 - SIZING_MULTIPLIER_MIN)
        elif trend == "bearish":
            if direction == "SELL":
                base_mult = 1.0 + strength * (SIZING_MULTIPLIER_MAX - 1.0)
            else:
                base_mult = 1.0 - strength * (1.0 - SIZING_MULTIPLIER_MIN)

    elif SIZING_STRATEGY == "equal" or grid_levels <= 0:
        base_mult = 1.0
    elif SIZING_STRATEGY in ("martingale", "anti_martingale", "pyramid"):
        half = grid_levels / 2
        dist = abs(level - half) / half if half > 0 else 0
        mn, mx = SIZING_MULTIPLIER_MIN, SIZING_MULTIPLIER_MAX
        if SIZING_STRATEGY == "martingale":
            base_mult = mn + (mx - mn) * dist
        elif SIZING_STRATEGY == "anti_martingale":
            base_mult = mx - (mx - mn) * dist
        elif SIZING_STRATEGY == "pyramid":
            base_mult = mx - (mx - mn) * dist

    return max(SIZING_MULTIPLIER_MIN, min(SIZING_MULTIPLIER_MAX, base_mult))


def _check_stop_conditions(state: dict, total_usd: float, price: float) -> str | None:
    """Check stop-loss, trailing-stop, and take-profit conditions."""
    stats = state.get("stats", {})
    initial = stats.get("initial_portfolio_usd")
    if not initial or initial <= 0:
        return None

    deposits = stats.get("total_deposits_usd", 0)
    cost_basis = initial + deposits

    peak = stats.get("portfolio_peak_usd", cost_basis)
    if total_usd > peak:
        peak = total_usd
        stats["portfolio_peak_usd"] = round(peak, 2)

    pnl_pct = (total_usd - cost_basis) / cost_basis

    if STOP_LOSS_PCT > 0 and pnl_pct <= -STOP_LOSS_PCT:
        return f"stop_loss ({pnl_pct * 100:+.1f}% <= -{STOP_LOSS_PCT * 100:.0f}%)"

    if TRAILING_STOP_PCT > 0 and peak > 0:
        drawdown = (peak - total_usd) / peak
        if drawdown >= TRAILING_STOP_PCT:
            return (
                f"trailing_stop (drawdown {drawdown * 100:.1f}% from peak ${peak:.0f})"
            )

    return None


def calc_trade_amount(
    direction: str,
    eth_bal: float,
    usdc_bal: float,
    price: float,
    current_level: int | None = None,
    grid_levels: int | None = None,
    mtf: dict | None = None,
) -> tuple[int | None, dict | None]:
    """Calculate trade amount. Returns (amount, failure_info)."""
    available_eth = eth_bal - GAS_RESERVE_ETH
    if available_eth < 0:
        available_eth = 0.0

    total_usd = available_eth * price + usdc_bal
    max_usd = total_usd * MAX_TRADE_PCT

    # Apply sizing strategy multiplier
    if current_level is not None and grid_levels is not None:
        multiplier = _calc_sizing_multiplier(current_level, grid_levels, direction, mtf)
        max_usd *= multiplier
        log(f"  Sizing: {direction} mult={multiplier:.2f} -> max_usd=${max_usd:.2f}")

    if max_usd < MIN_TRADE_USD:
        log(f"Trade too small: max ${max_usd:.2f} < min ${MIN_TRADE_USD}")
        return None, {
            "reason": "below_minimum",
            "detail": f"max ${max_usd:.2f} < min ${MIN_TRADE_USD}",
            "retriable": False,
            "hint": "amount_too_small",
        }

    if direction == "SELL":
        eth_to_sell = min(max_usd / price, available_eth)
        if eth_to_sell * price < MIN_TRADE_USD:
            return None, {
                "reason": "insufficient_balance",
                "detail": f"ETH to sell {eth_to_sell:.6f} below min",
                "retriable": False,
                "hint": "low_balance",
            }
        return int(eth_to_sell * 1e18), None
    else:
        usdc_to_spend = min(max_usd, usdc_bal * 0.95)
        if usdc_to_spend < MIN_TRADE_USD:
            return None, {
                "reason": "insufficient_balance",
                "detail": f"USDC ${usdc_to_spend:.2f} below min",
                "retriable": False,
                "hint": "low_balance",
            }
        return int(usdc_to_spend * 1e6), None


def ensure_approval(spender: str, amount: int) -> bool:
    """Ensure USDC approval for spender via onchainos."""
    state = load_state()
    approved_routers = state.get("approved_routers", [])
    if spender.lower() in [r.lower() for r in approved_routers]:
        return True

    log(f"USDC approval needed for {spender[:10]}... Approving...")
    max_approval = (
        "115792089237316195423570985008687907853269984665640564039457584007913129639935"
    )
    data = onchainos_cmd(
        [
            "swap",
            "approve",
            "--token",
            USDC_ADDR,
            "--amount",
            max_approval,
            "--chain",
            "base",
        ]
    )

    if not data or not data.get("ok") or not data.get("data"):
        log(f"Approve API failed: {json.dumps(data)[:200] if data else 'no response'}")
        return False

    approve_tx = data["data"][0]
    approve_tx["to"] = USDC_ADDR

    tx_hash, fail = _wallet_contract_call(approve_tx)
    if not tx_hash:
        log(f"Approval failed: {fail}")
        return False

    log(f"Approval TX: {tx_hash}")
    time.sleep(5)

    approved_routers.append(spender)
    state["approved_routers"] = approved_routers
    save_state(state)
    log(f"Router {spender[:10]}... added to approved list")
    return True


def _wallet_contract_call(tx: dict) -> tuple[str | None, dict | None]:
    """Sign + broadcast via onchainos wallet contract-call (TEE signing)."""
    value_wei = int(tx.get("value", "0"))
    value_eth = str(value_wei / 1e18) if value_wei > 0 else "0"

    args = [
        "wallet",
        "contract-call",
        "--to",
        tx["to"],
        "--chain",
        CHAIN_ID,
        "--input-data",
        tx.get("data", "0x"),
        "--value",
        value_eth,
    ]

    # Let onchainos estimate gas internally (tx.gas from DEX API is unreliable)
    if tx.get("gas"):
        gas_price_gwei = int(tx["gasPrice"]) / 1e9 if tx.get("gasPrice") else 0
        log(
            f"  TX gas hint (not used): dex_gas={tx['gas']}, gasPrice={gas_price_gwei:.2f} gwei"
        )

    try:
        data = onchainos_cmd(args, timeout=45)

        if data and data.get("ok") and data.get("data"):
            result = (
                data["data"]
                if isinstance(data["data"], dict)
                else (
                    data["data"][0] if isinstance(data["data"], list) else data["data"]
                )
            )
            tx_hash = (
                result.get("txHash") or result.get("hash") or result.get("orderId")
            )
            if tx_hash:
                log(f"  Broadcast OK: {tx_hash}")
                return tx_hash, None
            log(f"  Response missing hash: {json.dumps(result)[:300]}")
            return None, {
                "reason": "no_hash",
                "detail": json.dumps(result)[:200],
                "retriable": True,
                "hint": "transient_error",
            }

        detail = json.dumps(data)[:200] if data else "no response"
        log(f"  contract-call failed: {detail}")
        return None, {
            "reason": "contract_call_failed",
            "detail": detail,
            "retriable": True,
            "hint": "transient_error",
        }
    except Exception as e:
        return None, {
            "reason": "exception",
            "detail": str(e),
            "retriable": True,
            "hint": "transient_error",
        }


def simulate_tx(tx: dict) -> dict | None:
    """Simulate transaction via onchainos gateway simulate (non-blocking diagnostic)."""
    data = onchainos_cmd(
        [
            "gateway",
            "simulate",
            "--from",
            WALLET_ADDR,
            "--to",
            tx["to"],
            "--data",
            tx.get("data", "0x"),
            "--amount",
            tx.get("value", "0"),
            "--chain",
            "base",
        ],
        timeout=15,
    )
    if data and data.get("ok") and data.get("data"):
        sim = data["data"][0] if isinstance(data["data"], list) else data["data"]
        fail_reason = sim.get("failReason", "")
        gas_used = sim.get("gasUsed", "")
        success = not fail_reason
        log(
            f"  Simulation: {'OK' if success else 'FAIL'} gasUsed={gas_used}"
            + (f" reason={fail_reason}" if fail_reason else "")
        )
        return {"success": success, "failReason": fail_reason, "gasUsed": gas_used}
    if data:
        log(f"  Simulation error: {json.dumps(data)[:300]}")
    return None


def execute_swap(
    direction: str, amount: int, price: float, chain: str = "base"
) -> tuple[str | None, dict | None]:
    """Execute swap via onchainos CLI + Agentic Wallet (TEE signing)."""
    if direction == "SELL":
        from_token, to_token = ETH_ADDR, USDC_ADDR
    else:
        from_token, to_token = USDC_ADDR, ETH_ADDR

    for attempt in range(2):
        quote_time = time.time()

        swap_data = onchainos_cmd(
            [
                "swap",
                "swap",
                "--from",
                from_token,
                "--to",
                to_token,
                "--amount",
                str(amount),
                "--chain",
                chain,
                "--wallet",
                WALLET_ADDR,
                "--slippage",
                str(SLIPPAGE_PCT),
            ]
        )

        if not swap_data or not swap_data.get("ok") or not swap_data.get("data"):
            detail = json.dumps(swap_data)[:200] if swap_data else "no response"
            log(f"Swap quote failed (attempt {attempt + 1}): {detail}")
            if attempt == 0:
                time.sleep(3)
                continue
            return None, {
                "reason": "swap_quote_failed",
                "detail": detail,
                "retriable": True,
                "hint": "transient_api_error",
            }

        tx = swap_data["data"][0]["tx"]
        log(
            f"  OKX swap: to={tx['to'][:10]}... value={tx.get('value', '0')} "
            f"gas={tx.get('gas', 'N/A')} gasPrice={tx.get('gasPrice', 'N/A')}"
        )
        route_data = swap_data["data"][0]
        log(
            f"  Route: minReceive={route_data.get('minReceiveAmount', tx.get('minReceiveAmount', 'N/A'))} "
            f"slippage={SLIPPAGE_PCT}%"
        )

        sim_result = simulate_tx(tx)

        if direction == "BUY":
            router_addr = tx["to"]
            if not ensure_approval(router_addr, amount):
                log(f"Failed to approve USDC for router {router_addr}")
                return None, {
                    "reason": "approval_failed",
                    "detail": f"router {router_addr}",
                    "retriable": True,
                    "hint": "approval_might_be_pending",
                }

        elapsed = time.time() - quote_time
        log(f"  Time quote-to-submit: {elapsed:.1f}s")
        tx_hash, fail = _wallet_contract_call(tx)
        if tx_hash:
            return tx_hash, None

        if fail and sim_result:
            fail["simulation"] = sim_result

        log(f"Swap failed (attempt {attempt + 1}): {fail}")
        if (
            attempt == 0
            and fail
            and fail.get("hint")
            in ("network_timeout", "transient_error", "retry_with_fresh_quote")
        ):
            time.sleep(3)
            continue
        return None, fail

    return None, {
        "reason": "max_retries",
        "detail": "exhausted auto-retry",
        "retriable": True,
        "hint": "retry_with_fresh_quote",
    }


# ── State Management ────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "version": 5,
        "grid": None,
        "current_level": None,
        "price_history": [],
        "trades": [],
        "last_balances": None,
        "stats": {
            "total_sell_usdc": 0.0,
            "total_buy_usdc": 0.0,
            "realized_pnl": 0.0,
            "grid_profit": 0.0,
            "initial_portfolio_usd": None,
            "initial_eth_price": None,
            "started_at": datetime.now().isoformat(),
            "last_check": None,
            "trade_attempts": 0,
            "trade_successes": 0,
            "trade_failures": 0,
            "sell_attempts": 0,
            "sell_successes": 0,
            "buy_attempts": 0,
            "buy_successes": 0,
            "retry_attempts": 0,
            "retry_successes": 0,
            "total_deposits_usd": 0.0,
            "deposit_history": [],
        },
        "errors": {"consecutive": 0, "cooldown_until": None},
        "mtf_cache": None,
        "kline_cache": None,
        "sell_trail_counter": {},  # {level: tick_count}
    }


def save_state(state: dict):
    if STATE_FILE.exists():
        bak = STATE_FILE.with_suffix(".json.bak")
        bak.write_text(STATE_FILE.read_text())
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Market Analysis Helpers ─────────────────────────────────────────────────


def _calc_market_data(
    price: float,
    history: list[float],
    grid: dict,
    mtf: dict | None = None,
    kline_vol: float | None = None,
) -> dict:
    """Calculate market analysis data for JSON output."""
    ema = calc_ema(history, min(EMA_PERIOD, len(history))) if history else price
    vol = calc_volatility(history) if len(history) >= 2 else 0
    avg = sum(history) / len(history) if history else price
    vol_pct = round((vol / avg) * 100, 2) if avg else 0
    price_vs_ema = round(((price - ema) / ema) * 100, 2) if ema else 0

    grid_low, grid_high = grid["range"]
    grid_span = grid_high - grid_low
    grid_util = round((price - grid_low) / grid_span, 2) if grid_span > 0 else 0.5
    grid_util = max(0, min(1, grid_util))

    result = {
        "price": round(price, 2),
        "ema": round(ema, 2),
        "volatility_pct": vol_pct,
        "price_vs_ema_pct": price_vs_ema,
        "grid_utilization": grid_util,
    }

    # MTF data
    if mtf:
        result["trend"] = mtf.get("trend", "neutral")
        result["trend_strength"] = mtf.get("strength", 0)
        result["momentum_1h"] = mtf.get("momentum_1h", 0)
        result["momentum_4h"] = mtf.get("momentum_4h", 0)
        result["structure"] = mtf.get("structure", "ranging")
    else:
        # Fallback trend from v3 logic
        if len(history) >= 10:
            ema_short = calc_ema(history, min(5, len(history)))
            result["trend"] = (
                "bullish"
                if ema_short > ema * 1.001
                else "bearish"
                if ema_short < ema * 0.999
                else "neutral"
            )
        else:
            result["trend"] = "neutral"

    # K-line ATR volatility
    if kline_vol is not None:
        result["kline_atr_pct"] = round(kline_vol, 2)

    return result


def _emit_json(data: dict):
    """Print JSON block for AI agent parsing."""
    print("---JSON---")
    print(json.dumps(data, indent=2))


# ── Discord embed helper ────────────────────────────────────────────────────


def _resolve_discord_channel_id() -> str:
    """Resolve Discord channel ID: env override > openclaw.json guilds config."""
    env_id = os.environ.get("DISCORD_CHANNEL_ID", "")
    if env_id:
        return env_id
    try:
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            guilds = cfg.get("channels", {}).get("discord", {}).get("guilds", {})
            for guild_id, guild_cfg in guilds.items():
                channels = guild_cfg.get("channels", {})
                for ch_id, ch_cfg in channels.items():
                    if ch_cfg.get("allow"):
                        return ch_id
    except Exception:
        pass
    return ""


DISCORD_CHANNEL_ID = _resolve_discord_channel_id()


def _get_discord_token() -> str:
    env_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if env_token:
        return env_token
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        return cfg.get("channels", {}).get("discord", {}).get("token", "")
    return ""


def _send_discord_embed(embeds: list[dict], content: str = ""):
    """Send a Discord embed (card) message directly via Bot API."""
    import urllib.request
    import urllib.error

    token = _get_discord_token()
    if not token:
        log("Discord embed: no token found, falling back to print")
        return False
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    payload = {"embeds": embeds}
    if content:
        payload["content"] = content
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://openclaw.ai, 1.0)",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        log(f"Discord embed error: {e}")
        return False


def _record_attempt(state: dict, direction: str, success: bool, is_retry: bool = False):
    """Record a trade attempt for success rate tracking."""
    s = state["stats"]
    s["trade_attempts"] = s.get("trade_attempts", 0) + 1
    if success:
        s["trade_successes"] = s.get("trade_successes", 0) + 1
    else:
        s["trade_failures"] = s.get("trade_failures", 0) + 1
    dir_key = direction.lower()
    s[f"{dir_key}_attempts"] = s.get(f"{dir_key}_attempts", 0) + 1
    if success:
        s[f"{dir_key}_successes"] = s.get(f"{dir_key}_successes", 0) + 1
    if is_retry:
        s["retry_attempts"] = s.get("retry_attempts", 0) + 1
        if success:
            s["retry_successes"] = s.get("retry_successes", 0) + 1


def _success_rate_str(state: dict) -> str:
    s = state.get("stats", {})
    total = s.get("trade_attempts", 0)
    success = s.get("trade_successes", 0)
    if total == 0:
        return "N/A"
    rate = round(success / total * 100, 1)
    return f"{success}/{total} ({rate}%)"


def _success_rate_data(state: dict) -> dict:
    s = state.get("stats", {})
    total = s.get("trade_attempts", 0)
    success = s.get("trade_successes", 0)
    return {
        "total_attempts": total,
        "successes": success,
        "failures": s.get("trade_failures", 0),
        "rate_pct": round(success / total * 100, 1) if total > 0 else None,
        "sell": {
            "attempts": s.get("sell_attempts", 0),
            "successes": s.get("sell_successes", 0),
        },
        "buy": {
            "attempts": s.get("buy_attempts", 0),
            "successes": s.get("buy_successes", 0),
        },
        "retry": {
            "attempts": s.get("retry_attempts", 0),
            "successes": s.get("retry_successes", 0),
        },
    }


def _detect_deposits(
    state: dict, eth_bal: float, usdc_bal: float, price: float
) -> float | None:
    """Detect external deposits/withdrawals."""
    last = state.get("last_balances")
    if not last:
        return None

    last_time = last.get("time", "")
    delta_eth = eth_bal - last["eth"]
    delta_usdc = usdc_bal - last["usdc"]

    for t in state.get("trades", []):
        if t["time"] > last_time:
            trade_eth = t["amount_usd"] / t["price"]
            if t["direction"] == "SELL":
                delta_eth += trade_eth
                delta_usdc -= t["amount_usd"]
            else:
                delta_eth -= trade_eth
                delta_usdc += t["amount_usd"]

    deposit_usd = delta_eth * price + delta_usdc

    if abs(deposit_usd) > 100:
        event_type = "deposit" if deposit_usd > 0 else "withdrawal"
        event = {
            "time": datetime.now().isoformat(),
            "eth_delta": round(delta_eth, 6),
            "usdc_delta": round(delta_usdc, 2),
            "usd_value": round(deposit_usd, 2),
            "type": event_type,
        }

        dep_history = state["stats"].get("deposit_history", [])
        dep_history.append(event)
        if len(dep_history) > 20:
            dep_history = dep_history[-20:]
        state["stats"]["deposit_history"] = dep_history
        state["stats"]["total_deposits_usd"] = round(
            state["stats"].get("total_deposits_usd", 0) + deposit_usd, 2
        )

        type_cn = "存入" if deposit_usd > 0 else "取出"
        log(
            f"检测到{type_cn}: ~${abs(deposit_usd):.2f} (ETH {delta_eth:+.6f}, USDC {delta_usdc:+.2f})"
        )
        return deposit_usd

    return None


# ── Trend-Adaptive Position Limits ──────────────────────────────────────


def _get_position_limits(mtf: dict | None) -> tuple[int, int]:
    """Return (max_pct, min_pct) for position limits based on trend."""
    if not mtf:
        return POSITION_MAX_PCT_DEFAULT, POSITION_MIN_PCT_DEFAULT

    trend = mtf.get("trend", "neutral")
    strength = mtf.get("strength", 0)

    if trend == "bullish" and strength > 0.3:
        # Allow holding more ETH in bullish market
        max_pct = POSITION_MAX_PCT_DEFAULT + int(
            (POSITION_MAX_PCT_BULLISH - POSITION_MAX_PCT_DEFAULT) * strength
        )
        min_pct = POSITION_MIN_PCT_DEFAULT
        return max_pct, min_pct
    elif trend == "bearish" and strength > 0.3:
        max_pct = POSITION_MAX_PCT_DEFAULT
        min_pct = POSITION_MIN_PCT_DEFAULT - int(
            (POSITION_MIN_PCT_DEFAULT - POSITION_MIN_PCT_BEARISH) * strength
        )
        return max_pct, min_pct

    return POSITION_MAX_PCT_DEFAULT, POSITION_MIN_PCT_DEFAULT


# ── Sell Optimization ───────────────────────────────────────────────────


def _should_delay_sell(
    state: dict,
    current_level: int,
    prev_level: int,
    mtf: dict | None,
    history: list[float],
) -> str | None:
    """Check if we should delay sell in strong uptrend.
    Returns skip reason or None."""
    if not mtf:
        return None

    # If strong bullish momentum, delay sells to capture more upside
    momentum_1h = mtf.get("momentum_1h", 0)
    if momentum_1h > SELL_MOMENTUM_THRESHOLD * 100:
        trend = mtf.get("trend", "neutral")
        if trend == "bullish":
            # Bullish trend + strong momentum: skip this sell, let it ride
            structure = mtf.get("structure", "ranging")
            log(
                f"  sell delay: bullish momentum (1h momentum {momentum_1h:.2f}%, "
                f"structure={structure})"
            )
            return f"trend_hold (momentum +{momentum_1h:.1f}%)"

    # Trailing sell: wait a few ticks before selling to confirm reversal
    trail = state.get("sell_trail_counter", {})
    level_key = f"{prev_level}->{current_level}"
    count = trail.get(level_key, 0)
    if count < SELL_TRAIL_TICKS:
        trail[level_key] = count + 1
        state["sell_trail_counter"] = trail
        remaining = SELL_TRAIL_TICKS - count - 1
        log(f"  sell trail: waiting {remaining} more ticks for level {level_key}")
        return f"sell_trail ({count + 1}/{SELL_TRAIL_TICKS})"

    # Clear trail counter after triggering
    trail.pop(level_key, None)
    state["sell_trail_counter"] = trail
    return None


def _check_dip_buy(
    state: dict,
    price: float,
    history: list[float],
    eth_pct: float,
    mtf: dict | None,
) -> dict | None:
    """Check if we should dip-buy in accumulation mode.

    Triggers only when selling is structurally blocked (USDC-heavy).
    Returns {"multiplier": float, "drawdown": float, "reason": str} or None.
    """
    # Only activate when ETH% is below the sell threshold (buy-only regime)
    pos_max, pos_min = _get_position_limits(mtf)
    if eth_pct >= pos_min:
        return None  # can still sell → normal grid mode

    # Need enough history
    if len(history) < DIP_BUY_LOOKBACK:
        return None

    recent = history[-DIP_BUY_LOOKBACK:]
    recent_high = max(recent)
    if recent_high <= 0:
        return None

    drawdown = (recent_high - price) / recent_high

    # Condition 1: minimum pullback
    if drawdown < DIP_BUY_MIN_DRAWDOWN:
        return None

    # Condition 2: NOT falling hard (avoid catching knives)
    momentum_1h = mtf.get("momentum_1h", 0) if mtf else 0
    if momentum_1h < DIP_BUY_MOMENTUM_FLOOR:
        log(
            f"  dip-buy skip: momentum {momentum_1h:.2f}% < {DIP_BUY_MOMENTUM_FLOOR}% (falling knife)"
        )
        return None

    # Condition 3: reversal confirmation — price rising for N consecutive ticks
    tail = history[-DIP_BUY_REVERSAL_TICKS:]
    if len(tail) >= DIP_BUY_REVERSAL_TICKS:
        rising = all(tail[i] < tail[i + 1] for i in range(len(tail) - 1))
        if not rising:
            log(
                f"  dip-buy skip: no {DIP_BUY_REVERSAL_TICKS}-tick reversal "
                f"(last {len(tail)}: {[round(p, 1) for p in tail]})"
            )
            return None

    # Condition 4: cooldown since last dip-buy
    last_dip = state.get("last_dip_buy_time")
    if last_dip:
        elapsed = (datetime.now() - datetime.fromisoformat(last_dip)).total_seconds()
        if elapsed < DIP_BUY_COOLDOWN:
            return None

    # Determine tier
    for threshold, mult in DIP_BUY_TIERS:
        if drawdown >= threshold:
            return {
                "multiplier": mult,
                "drawdown": drawdown,
                "reason": f"dip_buy ({drawdown * 100:.1f}% from ${recent_high:.0f}, "
                f"momentum {momentum_1h:+.2f}%)",
            }

    return None


# ── Core Logic ──────────────────────────────────────────────────────────────


def tick():
    """Main tick: check price, execute trade if grid crossing detected.
    Multi-timeframe, sell-optimized."""
    state = load_state()

    # Circuit breaker check
    errors = state.get("errors", {})
    if errors.get("consecutive", 0) >= MAX_CONSECUTIVE_ERRORS:
        cooldown = errors.get("cooldown_until")
        if cooldown and datetime.fromisoformat(cooldown) > datetime.now():
            remaining = (
                datetime.fromisoformat(cooldown) - datetime.now()
            ).seconds // 60
            log(
                f"CIRCUIT BREAKER: {errors['consecutive']} consecutive errors. "
                f"Cooldown {remaining}min remaining."
            )
            _emit_json(
                {
                    "status": "circuit_breaker",
                    "retriable": False,
                    "hint": "cooldown_active",
                    "remaining_min": remaining,
                }
            )
            return
        else:
            log("Circuit breaker cooldown expired, resuming.")
            errors["consecutive"] = 0
            errors["cooldown_until"] = None

    # Get current price
    price = get_eth_price()
    if not price:
        errors["consecutive"] = errors.get("consecutive", 0) + 1
        if errors["consecutive"] >= MAX_CONSECUTIVE_ERRORS:
            errors["cooldown_until"] = (
                datetime.now() + timedelta(seconds=COOLDOWN_AFTER_ERRORS)
            ).isoformat()
            log(f"CIRCUIT BREAKER TRIGGERED after {errors['consecutive']} errors")
        state["errors"] = errors
        save_state(state)
        log("Failed to get price")
        _emit_json(
            {
                "status": "error",
                "reason": "price_fetch_failed",
                "retriable": True,
                "hint": "transient_api_error",
            }
        )
        return

    errors["consecutive"] = 0
    state["errors"] = errors

    # Update price history (keep last 288 = 24h at 5min intervals)
    history = state.get("price_history", [])
    history.append(price)
    if len(history) > 288:
        history = history[-288:]
    state["price_history"] = history

    # Get balances (fallback to last known if API fails)
    bal = get_balances()
    balance_failed = bal is None
    if bal is not None:
        eth_bal, usdc_bal = bal
    else:
        last_bal = state.get("last_balances", {})
        if last_bal.get("eth", 0) > 0 or last_bal.get("usdc", 0) > 0:
            eth_bal = last_bal.get("eth", 0)
            usdc_bal = last_bal.get("usdc", 0)
            log(
                f"Balance query failed — using last known: ETH={eth_bal}, USDC={usdc_bal}"
            )
        else:
            eth_bal, usdc_bal = 0.0, 0.0
    total_usd = eth_bal * price + usdc_bal

    # Snapshot initial portfolio on first tick
    if state["stats"].get("initial_portfolio_usd") is None:
        state["stats"]["initial_portfolio_usd"] = round(total_usd, 2)
        state["stats"]["initial_eth_price"] = round(price, 2)
        log(f"Initial portfolio snapshot: ${total_usd:.2f} @ ETH ${price:.2f}")

    # Detect external deposits/withdrawals (skip if balance query failed)
    detected_deposit = None
    if not balance_failed:
        detected_deposit = _detect_deposits(state, eth_bal, usdc_bal, price)

    # ── Multi-timeframe analysis ──
    mtf = analyze_multi_timeframe(history, price)
    state["mtf_cache"] = mtf

    # ── K-line volatility (fetch every 1h) ──
    kline_vol = None
    kline_cache = state.get("kline_cache")
    kline_stale = True
    if kline_cache and kline_cache.get("fetched_at"):
        elapsed = (
            datetime.now() - datetime.fromisoformat(kline_cache["fetched_at"])
        ).total_seconds()
        kline_stale = elapsed > 3600  # refresh hourly
    if kline_stale:
        candles = get_kline_data("1H", 24)
        if candles:
            kline_vol = calc_kline_volatility(candles)
            state["kline_cache"] = {
                "atr_pct": round(kline_vol, 3),
                "candles_count": len(candles),
                "fetched_at": datetime.now().isoformat(),
            }
            log(f"K-line ATR: {kline_vol:.2f}%")
        else:
            kline_vol = kline_cache.get("atr_pct") if kline_cache else None
    else:
        kline_vol = kline_cache.get("atr_pct") if kline_cache else None

    # ── Stop-loss / trailing-stop / take-profit guard ──
    if state.get("stop_triggered"):
        trigger = state["stop_triggered"]
        log(f"STOP ACTIVE: {trigger} -- trading halted")
        if not state.get("stop_notified"):
            state["stop_notified"] = True
            state.setdefault("stop_price", round(price, 2))
            state.setdefault("stop_time", datetime.now().isoformat())
            save_state(state)
            _send_discord_embed(
                [
                    {
                        "title": "\U0001f6d1 交易已停止",
                        "color": 0xFF0000,
                        "description": f"触发条件: **{trigger}**\n当前价格: ${price:.2f}\n组合价值: ${total_usd:.0f}\n\n将在价格回升后自动恢复",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )

        # ── Auto-resume check ──
        can_resume = False
        resume_reasons = []
        if STOP_AUTO_RESUME:
            stop_price = state.get("stop_price", price)
            stop_time_str = state.get("stop_time")
            cooldown_ok = True
            if stop_time_str:
                elapsed_min = (
                    datetime.now() - datetime.fromisoformat(stop_time_str)
                ).total_seconds() / 60
                cooldown_ok = elapsed_min >= STOP_COOLDOWN_MINUTES
                if not cooldown_ok:
                    resume_reasons.append(
                        f"冷却中 {elapsed_min:.0f}/{STOP_COOLDOWN_MINUTES}min"
                    )

            bounce_pct = (price - stop_price) / stop_price if stop_price > 0 else 0
            bounce_ok = bounce_pct >= STOP_RESUME_BOUNCE_PCT
            if not bounce_ok:
                resume_reasons.append(
                    f"反弹 {bounce_pct * 100:+.1f}% < {STOP_RESUME_BOUNCE_PCT * 100:.0f}%"
                )

            trend_ok = True
            if (
                mtf
                and mtf.get("trend") == "bearish"
                and mtf.get("strength", 0) >= STOP_RESUME_MAX_BEARISH
            ):
                trend_ok = False
                resume_reasons.append(
                    f"趋势仍强熊 strength={mtf.get('strength', 0):.2f}"
                )

            can_resume = cooldown_ok and bounce_ok and trend_ok

        if can_resume:
            old_trigger = state["stop_triggered"]
            state.pop("stop_triggered", None)
            state.pop("stop_notified", None)
            state.pop("stop_price", None)
            state.pop("stop_time", None)
            # Reset cost basis to current portfolio for fresh start
            state["stats"]["initial_portfolio_usd"] = round(total_usd, 2)
            state["stats"]["initial_eth_price"] = round(price, 2)
            state["stats"]["portfolio_peak_usd"] = round(total_usd, 2)
            state["stats"]["started_at"] = datetime.now().isoformat()
            state["grid"] = None  # force grid rebuild at current price
            save_state(state)
            log(f"AUTO-RESUME: conditions met, rebuilding grid at ${price:.2f}")
            _send_discord_embed(
                [
                    {
                        "title": "\u2705 自动恢复交易",
                        "color": 0x00C853,
                        "description": (
                            f"止损原因: {old_trigger}\n"
                            f"恢复价格: ${price:.2f} (反弹 {bounce_pct * 100:+.1f}%)\n"
                            f"新基准: ${total_usd:.0f}\n"
                            f"将以当前价格重建网格"
                        ),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )
            # Fall through to normal tick logic (grid rebuild etc.)
        else:
            reason_str = (
                ", ".join(resume_reasons) if resume_reasons else "auto-resume disabled"
            )
            log(f"Auto-resume not ready: {reason_str}")
            # Update balances to prevent repeated withdrawal detection
            state["last_balances"] = {
                "eth": round(eth_bal, 6),
                "usdc": round(usdc_bal, 2),
                "time": datetime.now().isoformat(),
            }
            save_state(state)
            _emit_json(
                {
                    "status": "stopped",
                    "stop_triggered": trigger,
                    "portfolio_usd": round(total_usd, 2),
                    "price": round(price, 2),
                    "auto_resume_pending": reason_str,
                }
            )
            return

    if balance_failed:
        log("Balance query failed — skipping stop checks this tick")
        stop_trigger = None
    else:
        stop_trigger = _check_stop_conditions(state, total_usd, price)
    if stop_trigger:
        state["stop_triggered"] = stop_trigger
        log(f"STOP TRIGGERED: {stop_trigger}")
        save_state(state)
        _send_discord_embed(
            [
                {
                    "title": "\U0001f6a8 止损/止盈触发!",
                    "color": 0xFF0000,
                    "description": f"**{stop_trigger}**\n价格: ${price:.2f}\n组合价值: ${total_usd:.0f}\n\n交易已自动停止。使用 `resume-trading` 恢复。",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        )
        _emit_json(
            {
                "status": "stop_triggered",
                "trigger": stop_trigger,
                "portfolio_usd": round(total_usd, 2),
                "price": round(price, 2),
            }
        )
        return

    # ── Grid stability: only recalibrate when needed ──
    grid = state.get("grid")
    grid_set_at = state.get("grid_set_at")
    need_recalibrate = False

    if not grid:
        need_recalibrate = True
    elif price < grid["range"][0] - grid.get("buy_step", grid["step"]):
        need_recalibrate = True
        state.pop("upside_breakout_ticks", None)
        log(f"Price ${price:.2f} BELOW grid {grid['range']} - recalibrating (downside)")
    elif price > grid["range"][1] + grid.get("sell_step", grid["step"]):
        ticks = state.get("upside_breakout_ticks", 0) + 1
        state["upside_breakout_ticks"] = ticks
        if ticks >= UPSIDE_CONFIRM_TICKS:
            need_recalibrate = True
            state.pop("upside_breakout_ticks", None)
            log(
                f"Price ${price:.2f} ABOVE grid {grid['range']} - recalibrating "
                f"(confirmed after {ticks} ticks)"
            )
        else:
            log(
                f"Price ${price:.2f} above grid {grid['range']} - "
                f"waiting confirmation ({ticks}/{UPSIDE_CONFIRM_TICKS})"
            )
    elif grid_set_at:
        hours_since = (
            datetime.now() - datetime.fromisoformat(grid_set_at)
        ).total_seconds() / 3600
        if hours_since > GRID_RECALIBRATE_HOURS:
            need_recalibrate = True
            log(
                f"Grid age {hours_since:.1f}h > {GRID_RECALIBRATE_HOURS}h - recalibrating"
            )
    else:
        need_recalibrate = True

    # Reset upside breakout counter if price is back inside grid
    if grid and grid["range"][0] <= price <= grid["range"][1] + grid.get(
        "sell_step", grid["step"]
    ):
        if state.get("upside_breakout_ticks", 0) > 0:
            log(f"Price ${price:.2f} back in grid range - reset upside counter")
            state.pop("upside_breakout_ticks", None)

    # Volatility shift detection (compare kline ATR to grid ATR, same source)
    if not need_recalibrate and grid and kline_vol is not None:
        grid_vol_pct = grid.get("vol_pct", 0)
        if grid_vol_pct > 0:
            vol_change_ratio = abs(kline_vol - grid_vol_pct) / grid_vol_pct
            if vol_change_ratio > VOL_RECALIBRATE_RATIO:
                need_recalibrate = True
                log(
                    f"Volatility shift: {grid_vol_pct:.2f}% -> {kline_vol:.2f}% "
                    f"(delta {vol_change_ratio * 100:.0f}%) - recalibrating"
                )

    if need_recalibrate:
        old_step = grid["step"] if grid else 0
        old_center = grid["center"] if grid else price
        grid = calc_dynamic_grid(price, history, mtf)

        # Cap grid center shift
        if old_center and old_center > 0:
            max_shift = old_center * MAX_CENTER_SHIFT_PCT
            new_center = grid["center"]
            if abs(new_center - old_center) > max_shift:
                capped_center = old_center + max_shift * (
                    1 if new_center > old_center else -1
                )
                log(
                    f"Center shift capped: ${new_center:.0f} -> ${capped_center:.0f} "
                    f"(max {MAX_CENTER_SHIFT_PCT * 100:.0f}% from ${old_center:.0f})"
                )
                grid["center"] = round(capped_center, 2)
                half_levels = int(grid["levels"] / 2)
                b_step = grid.get("buy_step", grid["step"])
                s_step = grid.get("sell_step", grid["step"])
                grid["level_prices"] = _build_level_prices(
                    capped_center,
                    b_step,
                    s_step,
                    half_levels,
                    grid.get("type", "arithmetic"),
                )
                grid["range"] = [grid["level_prices"][0], grid["level_prices"][-1]]

        state["grid"] = grid
        state["grid_set_at"] = datetime.now().isoformat()
        new_level = price_to_level(price, grid)
        old_level = state.get("current_level")
        # Only silence ±1 level drift from grid rebuild; preserve real jumps
        if old_level is None or abs(new_level - old_level) <= 1:
            state["current_level"] = new_level
        # Clear sell trail counters on recalibration
        state["sell_trail_counter"] = {}
        step_change = f" (was ${old_step:.1f})" if old_step else ""
        b_s = grid.get("buy_step", grid["step"])
        s_s = grid.get("sell_step", grid["step"])
        asym_info = f" buy=${b_s:.1f} sell=${s_s:.1f}" if abs(b_s - s_s) > 0.01 else ""
        log(
            f"Grid set: ${grid['range'][0]:.0f}-${grid['range'][1]:.0f} "
            f"step=${grid['step']:.1f}{asym_info}{step_change} "
            f"vol={grid.get('vol_pct', 0):.1f}% level={new_level}"
        )

    # Determine current grid level
    current_level = price_to_level(price, grid)
    prev_level = state.get("current_level")

    state["stats"]["last_check"] = datetime.now().isoformat()

    # Prepare output data
    market_data = _calc_market_data(price, history, grid, mtf, kline_vol)
    eth_pct = round((eth_bal * price / total_usd) * 100, 1) if total_usd > 0 else 0
    portfolio_data = {
        "eth": round(eth_bal, 6),
        "usdc": round(usdc_bal, 2),
        "total_usd": round(total_usd, 2),
        "eth_pct": eth_pct,
    }

    action = None
    tx_hash = None
    tick_status = "no_trade"
    failure_info = None
    direction = None

    if prev_level is not None and current_level != prev_level:
        direction = "SELL" if current_level > prev_level else "BUY"
        skip_reason = None

        # ── Zone filter: block BUY at top, block SELL at bottom ──
        buy_ceil = 4  # BUY allowed at L0-L4, blocked at L5-L6
        sell_floor = 1  # SELL allowed at L1-L6, blocked at L0
        if direction == "BUY" and current_level > buy_ceil:
            skip_reason = f"above buy-zone (L{current_level} > L{buy_ceil})"
            tick_status = "zone_filter"
        elif direction == "SELL" and current_level < sell_floor:
            skip_reason = f"below sell-zone (L{current_level} < L{sell_floor})"
            tick_status = "zone_filter"

        # ── Cooldown: min interval between same-direction trades ──
        last_trade_times = state.get("last_trade_times", {})
        last_time_str = last_trade_times.get(direction)
        if last_time_str:
            elapsed = (
                datetime.now() - datetime.fromisoformat(last_time_str)
            ).total_seconds()
            if elapsed < MIN_TRADE_INTERVAL:
                skip_reason = f"cooldown ({int(MIN_TRADE_INTERVAL - elapsed)}s left)"
                tick_status = "cooldown"

        # ── Cross-direction cooldown: prevent instant reversal after trade ──
        if not skip_reason:
            opposite = "SELL" if direction == "BUY" else "BUY"
            opp_time_str = last_trade_times.get(opposite)
            if opp_time_str:
                opp_elapsed = (
                    datetime.now() - datetime.fromisoformat(opp_time_str)
                ).total_seconds()
                if opp_elapsed < MIN_TRADE_INTERVAL:
                    skip_reason = (
                        f"cross-cooldown (last {opposite} {int(opp_elapsed)}s ago, "
                        f"need {MIN_TRADE_INTERVAL}s)"
                    )
                    tick_status = "cooldown"

        # ── Trend-adaptive position limits ──
        if not skip_reason:
            pos_max, pos_min = _get_position_limits(mtf)
            if direction == "BUY" and eth_pct > pos_max:
                skip_reason = f"ETH-heavy ({eth_pct:.0f}% > {pos_max}%)"
                tick_status = "position_limit"
            elif direction == "SELL" and eth_pct < pos_min:
                skip_reason = f"USDC-heavy ({eth_pct:.0f}% < {pos_min}%)"
                tick_status = "position_limit"

        # ── Anti-repeat: skip same boundary trade ──
        if not skip_reason:
            last_trade = state["trades"][-1] if state["trades"] else None
            if (
                last_trade
                and last_trade["direction"] == direction
                and last_trade["grid_to"] == current_level
                and last_trade["grid_from"] == prev_level
            ):
                skip_reason = "repeat boundary"
                tick_status = "skip_repeat"

        # ── Consecutive same-direction limit ──
        if not skip_reason:
            recent = state.get("trades", [])[-MAX_SAME_DIR_TRADES:]
            if len(recent) >= MAX_SAME_DIR_TRADES and all(
                t["direction"] == direction for t in recent
            ):
                last_trade_time = datetime.fromisoformat(recent[-1]["time"])
                grid_set_time = datetime.fromisoformat(
                    state.get("grid_set_at", recent[-1]["time"])
                )
                time_since_last = (datetime.now() - last_trade_time).total_seconds()
                grid_recalibrated = grid_set_time > last_trade_time
                if grid_recalibrated or time_since_last > 3600:
                    reason = (
                        "grid recalibrated"
                        if grid_recalibrated
                        else f"{time_since_last / 60:.0f}min elapsed"
                    )
                    log(f"Consecutive {direction} limit reset ({reason})")
                else:
                    skip_reason = (
                        f"consecutive {direction} limit ({MAX_SAME_DIR_TRADES})"
                    )
                    tick_status = "consecutive_limit"

        # ── Rapid drop detection: don't buy into a falling knife ──
        if not skip_reason and direction == "BUY":
            recent_prices = history[-6:]
            if len(recent_prices) >= 3:
                drop_pct = (
                    (recent_prices[-1] - max(recent_prices)) / max(recent_prices) * 100
                )
                if drop_pct < -2.0:
                    skip_reason = f"rapid drop ({drop_pct:.1f}% in 30min)"
                    tick_status = "rapid_drop"

        # ── Sell delay in strong uptrend ──
        if not skip_reason and direction == "SELL":
            sell_delay = _should_delay_sell(
                state, current_level, prev_level, mtf, history
            )
            if sell_delay:
                skip_reason = sell_delay
                tick_status = "sell_delayed"

        if skip_reason:
            log(f"SKIP {direction} L{prev_level}-L{current_level}: {skip_reason}")
            state["current_level"] = current_level
            direction = None
        else:
            # Clear sell trail counter on execution
            trail = state.get("sell_trail_counter", {})
            level_key = f"{prev_level}->{current_level}"
            trail.pop(level_key, None)
            state["sell_trail_counter"] = trail

            # Calculate trade amount
            amount, calc_fail = calc_trade_amount(
                direction,
                eth_bal,
                usdc_bal,
                price,
                current_level=current_level,
                grid_levels=GRID_LEVELS,
                mtf=mtf,
            )

            if amount:
                log(
                    f"GRID CROSSING: L{prev_level}-L{current_level} | "
                    f"${price:.2f} | {direction} | trend={mtf.get('trend', 'N/A')}"
                )
                tx_hash, swap_fail = execute_swap(direction, amount, price)

                if tx_hash:
                    _record_attempt(state, direction, True)
                    trade_usd = (
                        (amount / 1e18 * price)
                        if direction == "SELL"
                        else (amount / 1e6)
                    )
                    trade_eth = trade_usd / price if price else 0
                    if direction == "SELL":
                        # Use actual level_prices for accurate profit with asymmetric grids
                        lp = grid.get("level_prices", [])
                        if lp and prev_level < len(lp) and current_level < len(lp):
                            price_diff = abs(lp[prev_level] - lp[current_level])
                        else:
                            price_diff = abs(current_level - prev_level) * grid.get(
                                "step", 0
                            )
                        est_profit = round(price_diff * trade_eth, 2)
                    else:
                        est_profit = 0
                    trade_record = {
                        "time": datetime.now().isoformat(),
                        "direction": direction,
                        "price": round(price, 2),
                        "amount_usd": round(trade_usd, 2),
                        "est_profit": est_profit,
                        "tx": tx_hash,
                        "grid_from": prev_level,
                        "grid_to": current_level,
                        "trend": mtf.get("trend", "neutral"),
                        "trend_strength": mtf.get("strength", 0),
                    }
                    state["trades"].append(trade_record)
                    if len(state["trades"]) > 50:
                        state["trades"] = state["trades"][-50:]
                    if direction == "SELL":
                        state["stats"]["total_sell_usdc"] = round(
                            state["stats"].get("total_sell_usdc", 0) + trade_usd, 2
                        )
                    else:
                        state["stats"]["total_buy_usdc"] = round(
                            state["stats"].get("total_buy_usdc", 0) + trade_usd, 2
                        )
                    _total_usd = eth_bal * price + usdc_bal
                    _initial = state["stats"].get("initial_portfolio_usd") or 0
                    _deposits = state["stats"].get("total_deposits_usd", 0)
                    state["stats"]["realized_pnl"] = round(
                        _total_usd - _initial - _deposits, 2
                    )
                    state["stats"]["grid_profit"] = round(
                        state["stats"].get("grid_profit", 0) + est_profit, 2
                    )
                    dir_cn = "卖出" if direction == "SELL" else "买入"
                    profit_str = f" (利润 ~${est_profit:.2f})" if est_profit > 0 else ""
                    action = f"{dir_cn} ${trade_usd:.2f}{profit_str}"
                    tick_status = "trade_executed"
                    log(f"TX: https://basescan.org/tx/{tx_hash}")
                    state["current_level"] = current_level
                    if "last_trade_times" not in state:
                        state["last_trade_times"] = {}
                    state["last_trade_times"][direction] = datetime.now().isoformat()
                else:
                    _record_attempt(state, direction, False)
                    log(f"Trade execution failed: {swap_fail}")
                    dir_cn = "卖出" if direction == "SELL" else "买入"
                    action = f"{dir_cn} 失败"
                    tick_status = "trade_failed"
                    failure_info = swap_fail
                    state["last_failed_trade"] = {
                        "direction": direction,
                        "price": price,
                        "grid_from": prev_level,
                        "grid_to": current_level,
                        "time": datetime.now().isoformat(),
                    }
                    if failure_info:
                        _send_discord_embed(
                            [
                                {
                                    "title": "\u26a0\ufe0f 交易失败",
                                    "color": 0xFF9800,
                                    "fields": [
                                        {
                                            "name": "方向",
                                            "value": direction,
                                            "inline": True,
                                        },
                                        {
                                            "name": "价格",
                                            "value": f"${price:.2f}",
                                            "inline": True,
                                        },
                                        {
                                            "name": "原因",
                                            "value": failure_info.get(
                                                "reason", "unknown"
                                            ),
                                            "inline": True,
                                        },
                                        {
                                            "name": "详情",
                                            "value": str(
                                                failure_info.get("detail", "")
                                            )[:200],
                                            "inline": False,
                                        },
                                    ],
                                    "footer": {
                                        "text": f"可重试: {'是' if failure_info.get('retriable') else '否'}"
                                    },
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }
                            ]
                        )
            else:
                log(f"Skipped trade: {calc_fail}")
                action = f"{direction} skipped"
                tick_status = "trade_skipped"
                failure_info = calc_fail
    else:
        if prev_level is None:
            state["current_level"] = current_level
            log(f"Grid initialized at level {current_level}")
            tick_status = "initialized"
        else:
            # ── Dip-buy accumulation (no grid crossing, sell blocked) ──
            dip = _check_dip_buy(state, price, history, eth_pct, mtf)
            if dip:
                dip_mult = dip["multiplier"]
                dip_dd = dip["drawdown"]
                dip_reason = dip["reason"]
                log(f"DIP BUY: {dip_reason} mult={dip_mult:.1f}x")

                amount, calc_fail = calc_trade_amount(
                    "BUY",
                    eth_bal,
                    usdc_bal,
                    price,
                    current_level=current_level,
                    grid_levels=GRID_LEVELS,
                    mtf=mtf,
                )
                # Apply dip tier multiplier on top of trend sizing
                if amount:
                    amount = int(amount * dip_mult)
                    # Cap at 95% of USDC balance
                    max_amount = int(usdc_bal * 0.95 * 1e6)
                    amount = min(amount, max_amount)

                if amount and amount / 1e6 >= MIN_TRADE_USD:
                    tx_hash, swap_fail = execute_swap("BUY", amount, price)
                    if tx_hash:
                        _record_attempt(state, "BUY", True)
                        trade_usd = amount / 1e6
                        trade_record = {
                            "time": datetime.now().isoformat(),
                            "direction": "BUY",
                            "price": round(price, 2),
                            "amount_usd": round(trade_usd, 2),
                            "est_profit": 0,
                            "tx": tx_hash,
                            "grid_from": current_level,
                            "grid_to": current_level,
                            "trend": mtf.get("trend", "neutral") if mtf else "neutral",
                            "trend_strength": mtf.get("strength", 0) if mtf else 0,
                            "dip_buy": True,
                            "drawdown_pct": round(dip_dd * 100, 2),
                        }
                        state["trades"].append(trade_record)
                        if len(state["trades"]) > 50:
                            state["trades"] = state["trades"][-50:]
                        state["stats"]["total_buy_usdc"] = round(
                            state["stats"].get("total_buy_usdc", 0) + trade_usd, 2
                        )
                        _total_usd = eth_bal * price + usdc_bal
                        _initial = state["stats"].get("initial_portfolio_usd") or 0
                        _deposits = state["stats"].get("total_deposits_usd", 0)
                        state["stats"]["realized_pnl"] = round(
                            _total_usd - _initial - _deposits, 2
                        )
                        state["last_dip_buy_time"] = datetime.now().isoformat()
                        if "last_trade_times" not in state:
                            state["last_trade_times"] = {}
                        state["last_trade_times"]["BUY"] = datetime.now().isoformat()
                        action = f"逢低买入 ${trade_usd:.2f} (回撤{dip_dd * 100:.1f}%)"
                        tick_status = "dip_buy"
                        direction = "BUY"
                        log(f"TX: https://basescan.org/tx/{tx_hash}")
                    else:
                        _record_attempt(state, "BUY", False)
                        log(f"Dip buy failed: {swap_fail}")

    # Save balance snapshot
    state["last_balances"] = {
        "eth": round(eth_bal, 6),
        "usdc": round(usdc_bal, 2),
        "time": datetime.now().isoformat(),
    }
    save_state(state)

    # Output summary
    display_level = state.get("current_level", current_level)
    grid_range = f"${grid['range'][0]:.0f}-${grid['range'][1]:.0f}"
    initial = state["stats"].get("initial_portfolio_usd")
    deposits = state["stats"].get("total_deposits_usd", 0)
    cost_basis = (initial or 0) + deposits
    total_pnl = round(total_usd - cost_basis, 2) if initial else 0
    grid_profit = state["stats"].get("grid_profit", 0)
    trades_count = state["stats"].get("trade_successes", 0)
    has_event = bool(action or detected_deposit)

    # HODL comparison
    initial_price = state["stats"].get("initial_eth_price")
    hodl_alpha = None
    if initial and initial_price and initial_price > 0:
        initial_eth = initial / initial_price
        hodl_value = initial_eth * price
        hodl_alpha = round(total_usd - hodl_value, 2)

    # Quiet mode
    should_print = True
    if not has_event:
        last_quiet = state.get("last_quiet_report")
        now_iso = datetime.now().isoformat()
        if last_quiet:
            elapsed = (
                datetime.now() - datetime.fromisoformat(last_quiet)
            ).total_seconds()
            if elapsed < QUIET_INTERVAL:
                should_print = False
        if should_print:
            state["last_quiet_report"] = now_iso
            save_state(state)

    if should_print:
        pnl_emoji = "\U0001f7e2" if total_pnl >= 0 else "\U0001f534"
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        trend_str = mtf.get("trend", "?") if mtf else "?"
        vol_info = (
            f" | 波动 {grid.get('vol_pct', 0):.1f}%" if grid.get("vol_pct") else ""
        )
        trend_info = f" | 趋势 {trend_str}" if trend_str != "?" else ""
        grid_footer = (
            f"网格 {grid_range} | 步长 ${grid['step']:.1f}{vol_info}{trend_info}"
        )
        alpha_str = f" | Alpha ${hodl_alpha:+.2f}" if hodl_alpha is not None else ""

        if has_event:
            dir_cn = action if action else ""
            embed_color = 0x00C853 if "卖出" in dir_cn else 0x2979FF
            fields = [
                {"name": "价格", "value": f"${price:.2f}", "inline": True},
                {
                    "name": "层级",
                    "value": f"L{display_level}/{GRID_LEVELS}",
                    "inline": True,
                },
                {"name": "总值", "value": f"${total_usd:.0f}", "inline": True},
                {
                    "name": "持仓",
                    "value": f"{eth_bal:.4f} ETH + ${usdc_bal:.1f} USDC",
                    "inline": False,
                },
                {"name": "总收益", "value": pnl_str, "inline": True},
                {"name": "网格利润", "value": f"${grid_profit:+.2f}", "inline": True},
                {
                    "name": "交易次数",
                    "value": f"{trades_count} | [BaseScan](https://basescan.org/tx/{tx_hash})"
                    if tx_hash
                    else str(trades_count),
                    "inline": True,
                },
            ]
            if hodl_alpha is not None:
                fields.append(
                    {
                        "name": "HODL Alpha",
                        "value": f"${hodl_alpha:+.2f}",
                        "inline": True,
                    }
                )
            if mtf:
                fields.append(
                    {
                        "name": "趋势",
                        "value": f"{mtf.get('trend', 'N/A')} ({mtf.get('strength', 0):.0%})",
                        "inline": True,
                    }
                )
            if detected_deposit:
                dep_cn = "存入" if detected_deposit > 0 else "取出"
                fields.append(
                    {
                        "name": f"检测到{dep_cn}",
                        "value": f"${abs(detected_deposit):.2f} (已调整收益基准)",
                        "inline": False,
                    }
                )
            embed = {
                "title": f"\u26a1 {dir_cn}",
                "color": embed_color,
                "fields": fields,
                "footer": {"text": grid_footer},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        else:
            desc = (
                f"**${price:.2f}** | L{display_level}/{GRID_LEVELS} | "
                f"{eth_bal:.4f} ETH + ${usdc_bal:.1f} USDC = ${total_usd:.0f}\n"
                f"{pnl_emoji} 收益 {pnl_str} | 网格利润 ${grid_profit:+.2f} | {trades_count}笔{alpha_str}"
            )
            if deposits != 0:
                desc += f" | 资金调整 ${deposits:+.0f}"
            embed = {
                "title": "\u23f3 ETH 网格 v1.0 -- 运行中",
                "color": 0x9E9E9E,
                "description": desc,
                "footer": {"text": grid_footer},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Only send no-trade ticks on the hour; always send event ticks
        if has_event or datetime.now().minute < 5:
            sent = _send_discord_embed([embed])
        else:
            sent = False
        if not sent:
            pnl_sign = "+" if total_pnl >= 0 else ""
            summary = (
                f"**ETH** `${price:.2f}` | L`{display_level}`/`{GRID_LEVELS}` "
                f"| 网格 {grid_range} (步长 `${grid['step']:.1f}`) "
                f"| `{eth_bal:.4f}` ETH + `${usdc_bal:.1f}` USDC (`${total_usd:.0f}`)"
            )
            summary += f"\n> 总收益 `{pnl_sign}${total_pnl:.2f}` | 网格利润 `${grid_profit:+.2f}` | 交易 `{trades_count}` 笔{alpha_str}"
            if action:
                summary += f" | **{action}**"
                if tx_hash:
                    summary += f"\n<https://basescan.org/tx/{tx_hash}>"
            else:
                summary += " | 无交易"
            print(summary)

    # Output structured JSON for AI agent
    if should_print:
        json_data = {
            "status": tick_status,
            "version": "1.0",
            "market": market_data,
            "portfolio": portfolio_data,
            "grid_level": display_level,
            "prev_level": prev_level,
            "success_rate": _success_rate_data(state),
            "cost_basis": cost_basis,
            "total_deposits_usd": deposits,
            "hodl_alpha": hodl_alpha,
        }
        if detected_deposit:
            json_data["detected_deposit_usd"] = round(detected_deposit, 2)
        if direction:
            json_data["direction"] = direction
        if tx_hash:
            json_data["tx_hash"] = tx_hash
        if failure_info:
            json_data.update(
                {
                    "failure_reason": failure_info.get("reason", "unknown"),
                    "failure_detail": failure_info.get("detail", ""),
                    "retriable": failure_info.get("retriable", False),
                    "retry_hint": failure_info.get("hint", ""),
                }
            )
            if failure_info.get("simulation"):
                json_data["simulation"] = failure_info["simulation"]
        _emit_json(json_data)


# ── Sub-commands ────────────────────────────────────────────────────────────


def status():
    """Print current status."""
    state = load_state()
    price = get_eth_price()
    eth_bal, usdc_bal = get_balances()

    grid = state.get("grid")
    total_usd = eth_bal * (price or 0) + usdc_bal
    stats = state.get("stats", {})
    history = state.get("price_history", [])

    print("**ETH 网格机器人 v1.0 -- 状态**")
    print(f"> 价格: `${price:.2f}`" if price else "> 价格: 不可用")
    print(
        f"> 余额: `{eth_bal:.6f}` ETH + `${usdc_bal:.2f}` USDC = **`${total_usd:.0f}`**"
    )

    if grid:
        print(
            f"> 网格: `${grid['range'][0]:.0f}`-`${grid['range'][1]:.0f}` | "
            f"步长 `${grid['step']:.1f}` | 中心 `${grid['center']:.0f}`"
        )
        print(f"> 层级: `{state.get('current_level', '?')}`/`{GRID_LEVELS}`")
    else:
        print("> 网格: 未初始化")

    # MTF info
    if price and len(history) >= MTF_SHORT_PERIOD:
        mtf = analyze_multi_timeframe(history, price)
        print("\n**多时间框架分析**")
        print(
            f"> 趋势: `{mtf['trend']}` (强度 `{mtf['strength']:.0%}`) | 结构: `{mtf['structure']}`"
        )
        print(
            f"> 动量: 1h `{mtf['momentum_1h']:+.2f}%` | 4h `{mtf['momentum_4h']:+.2f}%`"
        )
        print(
            f"> EMA: 短 `${mtf['ema_short']:.1f}` | 中 `${mtf['ema_medium']:.1f}` | 长 `${mtf['ema_long']:.1f}`"
        )

    # PnL
    initial = stats.get("initial_portfolio_usd")
    deposits = stats.get("total_deposits_usd", 0)
    grid_profit = stats.get("grid_profit", 0)
    sell_total = stats.get("total_sell_usdc", 0)
    buy_total = stats.get("total_buy_usdc", 0)
    print("\n**收益统计**")
    if initial and price:
        cost_basis = initial + deposits
        total_pnl = round(total_usd - cost_basis, 2)
        holding_pnl = round(total_pnl - grid_profit, 2)
        pct = (total_pnl / cost_basis) * 100 if cost_basis else 0
        print(
            f"> 总收益: **`${total_pnl:+.2f}`** (`{pct:+.1f}%`)  起始 `${initial:.0f}`"
        )
        print(
            f"> 网格利润: `${grid_profit:+.2f}` (卖出赚差价) | 持仓浮盈: `${holding_pnl:+.2f}` (ETH涨跌)"
        )
        print(f"> 交易量: 卖出 `${sell_total:.2f}` | 买入 `${buy_total:.2f}`")

        # HODL comparison
        initial_price = stats.get("initial_eth_price")
        if initial_price and initial_price > 0:
            initial_eth = initial / initial_price
            hodl_value = initial_eth * price
            hodl_alpha = round(total_usd - hodl_value, 2)
            hodl_pct = round((price - initial_price) / initial_price * 100, 2)
            print(
                f"> HODL对比: ETH `{hodl_pct:+.1f}%` | HODL价值 `${hodl_value:.0f}` | **Alpha `${hodl_alpha:+.2f}`**"
            )

        if deposits != 0:
            print(f"> 资金调整: `${deposits:+.2f}` | 成本基准 `${cost_basis:.0f}`")

    # Success rate
    print(f"\n> 成功率: `{_success_rate_str(state)}`")

    # Stop status
    stop_trigger = state.get("stop_triggered")
    if stop_trigger:
        print(f"\n> \U0001f6d1 **交易已停止**: `{stop_trigger}`")
        print("> 使用 `resume-trading` 恢复交易")

    # Strategy info
    print("\n**策略配置 v1.0**")
    print(f"> 资金策略: `{SIZING_STRATEGY}` | 网格类型: `{GRID_TYPE}`")
    print(
        f"> 步长范围: `{STEP_MIN_PCT * 100:.1f}%`-`{STEP_MAX_PCT * 100:.1f}%` | 卖出追踪: `{SELL_TRAIL_TICKS}` ticks"
    )
    parts = []
    if STOP_LOSS_PCT > 0:
        parts.append(f"止损 {STOP_LOSS_PCT * 100:.0f}%")
    if TRAILING_STOP_PCT > 0:
        parts.append(f"追踪止损 {TRAILING_STOP_PCT * 100:.0f}%")
    if parts:
        peak = stats.get("portfolio_peak_usd")
        peak_str = f" | 峰值 `${peak:.0f}`" if peak else ""
        print(f"> 保护: {' | '.join(parts)}{peak_str}")


def report():
    """Generate daily report."""
    state = load_state()
    price = get_eth_price()
    eth_bal, usdc_bal = get_balances()
    total_usd = eth_bal * (price or 0) + usdc_bal

    stats = state.get("stats", {})
    trades = state.get("trades", [])
    grid = state.get("grid", {})
    history = state.get("price_history", [])

    today = datetime.now().date().isoformat()
    today_trades = [t for t in trades if t["time"].startswith(today)]

    if history:
        price_high = max(history)
        price_low = min(history)
        volatility = calc_volatility(history)
        vol_pct = (volatility / (sum(history) / len(history))) * 100 if history else 0
    else:
        price_high = price_low = price or 0
        vol_pct = 0

    # PnL
    initial = stats.get("initial_portfolio_usd")
    deposits = stats.get("total_deposits_usd", 0)
    grid_profit = stats.get("grid_profit", 0)
    sell_total = stats.get("total_sell_usdc", 0)
    buy_total = stats.get("total_buy_usdc", 0)

    # Build embed
    fields = [
        {
            "name": "当前价格",
            "value": f"${price:.2f}" if price else "N/A",
            "inline": True,
        },
        {
            "name": "24h 范围",
            "value": f"${price_low:.2f} - ${price_high:.2f}",
            "inline": True,
        },
        {"name": "波动率", "value": f"{vol_pct:.1f}%", "inline": True},
        {
            "name": "持仓",
            "value": f"{eth_bal:.4f} ETH + ${usdc_bal:.2f} USDC = **${total_usd:.0f}**",
            "inline": False,
        },
    ]

    if grid:
        fields.append(
            {
                "name": "网格",
                "value": f"${grid.get('range', [0, 0])[0]:.0f}-${grid.get('range', [0, 0])[1]:.0f} (步长 ${grid.get('step', 0):.1f}) | L{state.get('current_level', '?')}/{GRID_LEVELS}",
                "inline": False,
            }
        )

    if initial and price:
        cost_basis = initial + deposits
        total_pnl = round(total_usd - cost_basis, 2)
        pct = (total_pnl / cost_basis) * 100 if cost_basis else 0
        holding_pnl = round(total_pnl - grid_profit, 2)
        fields.append(
            {
                "name": "总收益",
                "value": f"${total_pnl:+.2f} ({pct:+.1f}%)",
                "inline": True,
            }
        )
        fields.append(
            {"name": "网格利润", "value": f"${grid_profit:+.2f}", "inline": True}
        )
        fields.append(
            {"name": "持仓浮盈", "value": f"${holding_pnl:+.2f}", "inline": True}
        )
        fields.append(
            {
                "name": "交易量",
                "value": f"卖出 ${sell_total:.2f} | 买入 ${buy_total:.2f}",
                "inline": False,
            }
        )

        initial_price = stats.get("initial_eth_price")
        if initial_price and initial_price > 0:
            initial_eth = initial / initial_price
            hodl_value = initial_eth * price
            hodl_alpha = round(total_usd - hodl_value, 2)
            fields.append(
                {"name": "HODL Alpha", "value": f"${hodl_alpha:+.2f}", "inline": True}
            )

    fields.append(
        {"name": "成功率", "value": f"{_success_rate_str(state)}", "inline": True}
    )
    fields.append(
        {
            "name": "累计交易",
            "value": f"{stats.get('trade_successes', 0)} 笔",
            "inline": True,
        }
    )

    # Today's trades summary
    if today_trades:
        trade_lines = []
        for t in today_trades[-10:]:
            dir_cn = "卖出" if t["direction"] == "SELL" else "买入"
            est = t.get("est_profit", 0)
            profit_str = f" ~${est:.2f}" if est > 0 else ""
            trade_lines.append(
                f"`{t['time'][11:19]}` {dir_cn} ${t['amount_usd']:.2f} @ ${t['price']:.2f}{profit_str}"
            )
        fields.append(
            {
                "name": f"今日交易 ({len(today_trades)}笔)",
                "value": "\n".join(trade_lines),
                "inline": False,
            }
        )
    else:
        fields.append({"name": "今日交易", "value": "暂无", "inline": True})

    # MTF info in footer
    footer_text = f"运行自 {stats.get('started_at', '未知')[:10]}"
    if price and len(history) >= MTF_SHORT_PERIOD:
        mtf = analyze_multi_timeframe(history, price)
        footer_text = f"趋势 {mtf['trend']} ({mtf['strength']:.0%}) | 结构 {mtf['structure']} | {footer_text}"

    embed = {
        "title": "\U0001f4ca ETH 网格 v1.0 — 每日报告",
        "color": 0x2196F3,
        "fields": fields,
        "footer": {"text": footer_text},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    sent = _send_discord_embed([embed])
    if not sent:
        # Fallback to print output
        print("**ETH 网格机器人 v1.0 -- 每日报告**")
        print(f"> 当前价格: `${price:.2f}`" if price else "> 当前价格: N/A")
        print(
            f"> 24h 范围: `${price_low:.2f}` - `${price_high:.2f}` | 波动率: `{vol_pct:.1f}%`"
        )

        if price and len(history) >= MTF_SHORT_PERIOD:
            if not mtf:
                mtf = analyze_multi_timeframe(history, price)
            print(
                f"> 趋势: `{mtf['trend']}` ({mtf['strength']:.0%}) | 结构: `{mtf['structure']}`"
            )
            print(
                f"> 动量: 1h `{mtf['momentum_1h']:+.2f}%` | 4h `{mtf['momentum_4h']:+.2f}%`"
            )

        print("\n**持仓**")
        print(
            f"> `{eth_bal:.4f}` ETH + `${usdc_bal:.2f}` USDC = **`${total_usd:.0f}`**"
        )
        if grid:
            print(
                f"> 网格: `${grid.get('range', [0, 0])[0]:.0f}`-`${grid.get('range', [0, 0])[1]:.0f}` "
                f"(步长 `${grid.get('step', 0):.1f}`) | 层级 `{state.get('current_level', '?')}`/`{GRID_LEVELS}`"
            )

        print("\n**收益**")
        if initial and price:
            print(
                f"> 总收益: **`${total_pnl:+.2f}` (`{pct:+.1f}%`)**  起始 `${initial:.0f}`"
            )
            print(
                f"> 网格利润: `${grid_profit:+.2f}` | 持仓浮盈: `${holding_pnl:+.2f}`"
            )
            print(f"> 交易量: 卖出 `${sell_total:.2f}` | 买入 `${buy_total:.2f}`")

            initial_price = stats.get("initial_eth_price")
            if initial_price and initial_price > 0:
                initial_eth = initial / initial_price
                hodl_value = initial_eth * price
                hodl_alpha = round(total_usd - hodl_value, 2)
                print(f"> **HODL Alpha: `${hodl_alpha:+.2f}`**")

        print(f"\n> 成功率: `{_success_rate_str(state)}`")

        print(f"\n**今日交易: `{len(today_trades)}` 笔**")
        if today_trades:
            for t in today_trades[-10:]:
                dir_cn = "卖出" if t["direction"] == "SELL" else "买入"
                est = t.get("est_profit", 0)
                profit_str = f" 利润~`${est:.2f}`" if est > 0 else ""
                trend = t.get("trend", "")
                trend_str = f" [{trend}]" if trend else ""
                print(
                    f"> `{t['time'][11:19]}` {dir_cn} `${t['amount_usd']:.2f}` @ `${t['price']:.2f}`{profit_str}{trend_str}"
                )
        else:
            print("> 今日暂无交易")

        print(
            f"\n> 累计交易: `{stats.get('trade_successes', 0)}` 笔 | 运行自: `{stats.get('started_at', '未知')[:10]}`"
        )


def history_cmd():
    """Show recent trade history."""
    state = load_state()
    trades = state.get("trades", [])
    if not trades:
        print("暂无交易记录")
        return

    print(f"**最近 `{len(trades)}` 笔交易**")
    for t in trades:
        dir_cn = "卖出" if t["direction"] == "SELL" else "买入"
        est = t.get("est_profit", 0)
        profit_str = f" | 利润~`${est:.2f}`" if est > 0 else ""
        trend = t.get("trend", "")
        trend_str = f" [{trend}]" if trend else ""
        print(
            f"> `{t['time'][:19]}` | {dir_cn} `${t['amount_usd']:>8.2f}` "
            f"@ `${t['price']:.2f}` | L`{t['grid_from']}`->L`{t['grid_to']}`{profit_str}{trend_str} "
            f"| `{t['tx'][:16]}...`"
        )


def reset():
    """Reset state (recalibrate grid from scratch)."""
    price = get_eth_price()
    eth_bal, usdc_bal = get_balances()

    state = load_state()
    old_trades = state.get("trades", [])
    old_stats = state.get("stats", {})

    new_state = {
        "version": 5,
        "grid": None,
        "current_level": None,
        "price_history": [price] if price else [],
        "trades": old_trades,
        "stats": {
            "total_sell_usdc": old_stats.get("total_sell_usdc", 0.0),
            "total_buy_usdc": old_stats.get("total_buy_usdc", 0.0),
            "realized_pnl": old_stats.get("realized_pnl", 0.0),
            "grid_profit": old_stats.get("grid_profit", 0.0),
            "initial_portfolio_usd": None,
            "initial_eth_price": None,
            "started_at": datetime.now().isoformat(),
            "last_check": datetime.now().isoformat(),
            "trade_attempts": 0,
            "trade_successes": 0,
            "trade_failures": 0,
            "sell_attempts": 0,
            "sell_successes": 0,
            "buy_attempts": 0,
            "buy_successes": 0,
            "retry_attempts": 0,
            "retry_successes": 0,
            "total_deposits_usd": 0.0,
            "deposit_history": [],
        },
        "errors": {"consecutive": 0, "cooldown_until": None},
        "last_failed_trade": None,
        "last_balances": None,
        "grid_set_at": None,
        "last_trade_times": {},
        "mtf_cache": None,
        "kline_cache": None,
        "sell_trail_counter": {},
    }
    save_state(new_state)

    total = eth_bal * (price or 0) + usdc_bal
    print(f"网格已重置 (v1.0)。价格: `${price:.2f}`, 余额: `${total:.0f}`")
    print("计数器已重置。下次 tick 时将重新校准网格。")


def retry():
    """Retry the last failed trade with a fresh quote."""
    state = load_state()
    last_fail = state.get("last_failed_trade")

    if not last_fail:
        print("无需重试")
        _emit_json({"status": "no_retry_needed"})
        return

    fail_time = datetime.fromisoformat(last_fail["time"])
    if (datetime.now() - fail_time).total_seconds() > 600:
        print("上次失败交易已超过10分钟，跳过重试")
        _emit_json({"status": "retry_expired", "failed_at": last_fail["time"]})
        state.pop("last_failed_trade", None)
        save_state(state)
        return

    direction = last_fail["direction"]
    dir_cn = "卖出" if direction == "SELL" else "买入"
    price = get_eth_price()
    if not price:
        print("无法重试: 价格不可用")
        _emit_json({"status": "error", "reason": "price_fetch_failed"})
        return

    eth_bal, usdc_bal = get_balances()
    amount, calc_fail = calc_trade_amount(direction, eth_bal, usdc_bal, price)

    if not amount:
        print(f"无法重试: {calc_fail}")
        _emit_json(
            {"status": "retry_failed", "reason": calc_fail.get("reason", "unknown")}
        )
        return

    log(
        f"RETRY: {direction} at ${price:.2f} (original fail at ${last_fail['price']:.2f})"
    )
    tx_hash, swap_fail = execute_swap(direction, amount, price)

    if tx_hash:
        _record_attempt(state, direction, True, is_retry=True)
        trade_usd = (amount / 1e18 * price) if direction == "SELL" else (amount / 1e6)
        trade_record = {
            "time": datetime.now().isoformat(),
            "direction": direction,
            "price": round(price, 2),
            "amount_usd": round(trade_usd, 2),
            "tx": tx_hash,
            "grid_from": last_fail["grid_from"],
            "grid_to": last_fail["grid_to"],
            "trend": "retry",
        }
        state["trades"].append(trade_record)
        if len(state["trades"]) > 50:
            state["trades"] = state["trades"][-50:]
        if direction == "SELL":
            state["stats"]["total_sell_usdc"] = round(
                state["stats"].get("total_sell_usdc", 0) + trade_usd, 2
            )
        else:
            state["stats"]["total_buy_usdc"] = round(
                state["stats"].get("total_buy_usdc", 0) + trade_usd, 2
            )
        _total_usd = eth_bal * price + usdc_bal
        _initial = state["stats"].get("initial_portfolio_usd") or 0
        _deposits = state["stats"].get("total_deposits_usd", 0)
        state["stats"]["realized_pnl"] = round(_total_usd - _initial - _deposits, 2)
        # Update trade time to enforce cross-direction cooldown
        if "last_trade_times" not in state:
            state["last_trade_times"] = {}
        state["last_trade_times"][direction] = datetime.now().isoformat()
        state.pop("last_failed_trade", None)
        save_state(state)
        print(f"**重试成功**: {dir_cn} `${trade_usd:.2f}` @ `${price:.2f}`")
        log(f"RETRY TX: https://basescan.org/tx/{tx_hash}")
        # Discord notification for retry success
        retry_embed = {
            "title": f"\u267b\ufe0f 重试{dir_cn}成功",
            "color": 0x00C853 if direction == "SELL" else 0x2979FF,
            "fields": [
                {"name": "价格", "value": f"${price:.2f}", "inline": True},
                {"name": "金额", "value": f"${trade_usd:.2f}", "inline": True},
                {
                    "name": "层级",
                    "value": f"L{last_fail['grid_from']}→L{last_fail['grid_to']}",
                    "inline": True,
                },
                {
                    "name": "交易",
                    "value": f"[BaseScan](https://basescan.org/tx/{tx_hash})",
                    "inline": False,
                },
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _send_discord_embed([retry_embed])
        _emit_json(
            {
                "status": "retry_success",
                "direction": direction,
                "tx_hash": tx_hash,
                "amount_usd": round(trade_usd, 2),
            }
        )
    else:
        _record_attempt(state, direction, False, is_retry=True)
        save_state(state)
        print(
            f"**重试失败**: {dir_cn} -- {swap_fail.get('reason', '未知') if swap_fail else '未知'}"
        )
        log(f"RETRY FAILED: {swap_fail}")
        _emit_json(
            {
                "status": "retry_failed",
                "direction": direction,
                "failure_reason": swap_fail.get("reason", "unknown")
                if swap_fail
                else "unknown",
            }
        )


def analyze():
    """Output detailed market analysis JSON for AI agent."""
    state = load_state()
    price = get_eth_price()
    eth_bal, usdc_bal = get_balances()
    history = state.get("price_history", [])
    grid = state.get("grid")
    trades = state.get("trades", [])
    stats = state.get("stats", {})

    if not price:
        print(json.dumps({"error": "price_unavailable"}))
        return

    total_usd = eth_bal * price + usdc_bal

    # MTF analysis
    mtf = analyze_multi_timeframe(history, price)

    # K-line ATR
    candles = get_kline_data("1H", 24)
    kline_vol = calc_kline_volatility(candles) if candles else None

    # Price changes
    def pct_change(window):
        if len(history) < window:
            return None
        old = history[-window]
        return round(((price - old) / old) * 100, 2)

    price_changes = {
        "1h": pct_change(12),
        "4h": pct_change(48),
        "24h": pct_change(288),
    }

    # Volatility trend
    vol_recent = calc_volatility(history[-24:]) if len(history) >= 24 else None
    vol_older = calc_volatility(history[-72:-24]) if len(history) >= 72 else None
    if vol_recent and vol_older and vol_older > 0:
        vol_trend = (
            "increasing"
            if vol_recent > vol_older * 1.2
            else "decreasing"
            if vol_recent < vol_older * 0.8
            else "stable"
        )
    else:
        vol_trend = "insufficient_data"

    # Grid efficiency
    grid_efficiency = None
    if grid and len(history) >= 12:
        grid_low, grid_high = grid["range"]
        recent = history[-12:]
        in_grid = sum(1 for p in recent if grid_low <= p <= grid_high)
        grid_efficiency = round(in_grid / len(recent), 2)

    # HODL alpha
    initial = stats.get("initial_portfolio_usd")
    initial_price = stats.get("initial_eth_price")
    hodl_alpha = None
    if initial and initial_price and initial_price > 0:
        initial_eth = initial / initial_price
        hodl_value = initial_eth * price
        hodl_alpha = round(total_usd - hodl_value, 2)

    # Round trip analysis (last 20 trades)
    round_trips = []
    buy_stack = []
    for t in trades[-20:]:
        if t["direction"] == "BUY":
            buy_stack.append(t)
        else:
            for j in range(len(buy_stack) - 1, -1, -1):
                if buy_stack[j]["grid_to"] == t["grid_from"]:
                    matched_buy = buy_stack.pop(j)
                    spread = (
                        (t["price"] - matched_buy["price"]) / matched_buy["price"] * 100
                    )
                    hold_min = 0
                    try:
                        hold_min = int(
                            (
                                datetime.fromisoformat(t["time"])
                                - datetime.fromisoformat(matched_buy["time"])
                            ).total_seconds()
                            / 60
                        )
                    except Exception:
                        pass
                    round_trips.append(
                        {
                            "buy_price": matched_buy["price"],
                            "sell_price": t["price"],
                            "spread_pct": round(spread, 3),
                            "hold_min": hold_min,
                            "status": "good"
                            if spread >= 0.3
                            else "micro"
                            if spread > 0
                            else "loss",
                        }
                    )
                    break

    analysis = {
        "version": "1.0",
        "timestamp": datetime.now().isoformat(),
        "market": {
            "price": round(price, 2),
            "ema_20": round(calc_ema(history, min(EMA_PERIOD, len(history))), 2)
            if history
            else price,
            "price_changes": price_changes,
            "volatility_pct": round(
                (calc_volatility(history) / (sum(history) / len(history))) * 100, 2
            )
            if len(history) >= 2
            else 0,
            "volatility_trend": vol_trend,
            "kline_atr_pct": round(kline_vol, 2) if kline_vol else None,
        },
        "multi_timeframe": mtf,
        "portfolio": {
            "eth": round(eth_bal, 6),
            "usdc": round(usdc_bal, 2),
            "total_usd": round(total_usd, 2),
            "eth_pct": round((eth_bal * price / total_usd) * 100, 1)
            if total_usd > 0
            else 0,
        },
        "pnl": {
            "total_pnl": round(
                total_usd - ((initial or 0) + stats.get("total_deposits_usd", 0)), 2
            ),
            "grid_profit": stats.get("grid_profit", 0),
            "hodl_alpha": hodl_alpha,
        },
        "grid": {
            "range": grid["range"] if grid else None,
            "step": grid["step"] if grid else None,
            "level": state.get("current_level"),
            "efficiency": grid_efficiency,
        },
        "round_trips": {
            "count": len(round_trips),
            "good": sum(1 for r in round_trips if r["status"] == "good"),
            "micro": sum(1 for r in round_trips if r["status"] == "micro"),
            "loss": sum(1 for r in round_trips if r["status"] == "loss"),
            "avg_spread_pct": round(
                sum(r["spread_pct"] for r in round_trips) / len(round_trips), 3
            )
            if round_trips
            else 0,
            "details": round_trips[-5:],  # last 5
        },
        "success_rate": _success_rate_data(state),
    }

    print(json.dumps(analysis, indent=2))


def deposit():
    """Manually record deposit/withdrawal."""
    if len(sys.argv) < 3:
        print("用法: eth_grid_v1.py deposit <金额USD>")
        print("正数=存入, 负数=取出. 例: deposit 100 或 deposit -50")
        return

    try:
        amount = float(sys.argv[2])
    except ValueError:
        print("无效金额")
        return

    state = load_state()
    event = {
        "time": datetime.now().isoformat(),
        "usd_value": round(amount, 2),
        "type": "manual_deposit" if amount > 0 else "manual_withdrawal",
    }
    dep_history = state["stats"].get("deposit_history", [])
    dep_history.append(event)
    state["stats"]["deposit_history"] = dep_history
    state["stats"]["total_deposits_usd"] = round(
        state["stats"].get("total_deposits_usd", 0) + amount, 2
    )
    save_state(state)

    type_cn = "存入" if amount > 0 else "取出"
    print(f"已记录{type_cn}: ${abs(amount):.2f}")


def resume_trading():
    """Clear stop_triggered flag and resume trading."""
    state = load_state()
    if not state.get("stop_triggered"):
        print("交易未停止，无需恢复")
        return
    old_trigger = state["stop_triggered"]
    state.pop("stop_triggered", None)
    state.pop("stop_notified", None)
    save_state(state)
    log(f"Trading resumed (was: {old_trigger})")
    print(f"交易已恢复 (之前停止原因: {old_trigger})")
    _send_discord_embed(
        [
            {
                "title": "\u2705 交易已恢复",
                "color": 0x00C853,
                "description": f"之前停止原因: {old_trigger}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )


# ── Main ────────────────────────────────────────────────────────────────────

COMMANDS = {
    "tick": tick,
    "status": status,
    "report": report,
    "history": history_cmd,
    "reset": reset,
    "retry": retry,
    "analyze": analyze,
    "deposit": deposit,
    "resume-trading": resume_trading,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    handler = COMMANDS.get(cmd)
    if handler:
        handler()
    else:
        print(f"未知命令: {cmd}")
        print(f"可用命令: {', '.join(COMMANDS.keys())}")
        sys.exit(1)
