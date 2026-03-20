# Grid Algorithm Reference

Detailed explanation of the core algorithms in Grid Trading v1.0.

## 1. Multi-Timeframe Analysis (MTF)

MTF provides trend context to all downstream decisions: grid width, position sizing, sell delay, and position limits.

### EMA Hierarchy

Three exponential moving averages computed from the 5-minute price history:

| EMA | Period | Timeframe | Role |
|-----|--------|-----------|------|
| Short | 5 bars | 25 minutes | Immediate price direction |
| Medium | 12 bars | 1 hour | Intraday trend |
| Long | 48 bars | 4 hours | Macro trend |

**EMA calculation**:
```python
def ema(prices, period):
    k = 2 / (period + 1)
    result = prices[0]
    for p in prices[1:]:
        result = p * k + result * (1 - k)
    return result
```

### Trend Detection

**Alignment-based**:
- `short > medium > long` -> bullish
- `short < medium < long` -> bearish
- Otherwise -> neutral

**Strength** (0 to 1):
```
spread = (short - long) / long
strength = clamp(abs(spread) / 0.02, 0, 1)
```
A 2% spread between short and long EMA gives maximum strength (1.0).

### Structure Detection (8h Window)

Uses 96 bars (8 hours at 5-min intervals), split into 4 equal segments of 24 bars each.

```
Segment highs:  H1, H2, H3, H4
Segment lows:   L1, L2, L3, L4

If H1 < H2 < H3 < H4 AND L1 < L2 < L3 < L4 -> "uptrend"
If H1 > H2 > H3 > H4 AND L1 > L2 > L3 > L4 -> "downtrend"
Else -> "ranging"
```

### Momentum

```
momentum_1h = (price - price_12_bars_ago) / price_12_bars_ago * 100
momentum_4h = (price - price_48_bars_ago) / price_48_bars_ago * 100
```

### Output

```python
mtf = {
    "trend": "bullish" | "bearish" | "neutral",
    "strength": 0.0 - 1.0,
    "momentum_1h": float,   # percentage
    "momentum_4h": float,   # percentage
    "structure": "uptrend" | "downtrend" | "ranging",
    "ema_short": float,
    "ema_medium": float,
    "ema_long": float
}
```

---

## 2. K-line ATR Volatility

Supplements the price history stddev with OHLC-based volatility from 1-hour candles.

### True Range

For each candle:
```
TR = max(
    high - low,
    abs(high - prev_close),
    abs(low - prev_close)
)
```

### ATR

```
ATR = mean(TR[i] for i in last N candles)
atr_pct = ATR / current_price * 100
```

### Usage in Grid Calculation

When K-line data is available, `kline_atr_pct` is blended with the price history stddev to get a more robust volatility estimate. The ATR captures intra-candle volatility that tick-based stddev may miss.

Cache TTL: 1 hour.

---

## 3. Dynamic Grid Calculation

### Grid Center

```python
# Prefer 1H kline for grid center (more robust than 5min ticks)
candles = get_kline_data(bar="1H", limit=EMA_PERIOD)  # 20 hourly candles
if candles:
    center = EMA([c.close for c in candles], EMA_PERIOD)  # 20-hour EMA
else:
    center = EMA(price_history, EMA_PERIOD)  # fallback: 5min tick history
```

Grid center uses 1H kline EMA (20h lookback) instead of 5min tick EMA (100min lookback). This produces a more stable center that doesn't drift on short-term noise, better matching the 12-hour recalibration cycle.

### Trend-Adaptive Volatility Multiplier

```python
vol_mult = VOLATILITY_MULTIPLIER_BASE  # 2.0

if mtf and mtf["strength"] > 0.3:
    vol_mult = BASE + (TREND - BASE) * strength
    # Range: 2.0 to 3.0
```

**Effect**: In strong trends, the grid becomes wider -> fewer trades -> bot holds position longer -> captures trend moves instead of selling too early.

### Asymmetric Buy/Sell Steps

```python
# Asymmetry scales with trend strength (only active when strength > 0.3)
asym = ASYM_FACTOR * strength  # ASYM_FACTOR = 0.4

if trend == "bullish":
    buy_mult  = vol_mult * (1 - asym)   # tighter → accumulate fast
    sell_mult = vol_mult * (1 + asym)   # wider → hold longer
elif trend == "bearish":
    buy_mult  = vol_mult * (1 + asym)   # wider → wait for deeper dips
    sell_mult = vol_mult * (1 - asym)   # tighter → exit fast
else:
    buy_mult = sell_mult = vol_mult     # symmetric
```

**Example** (bullish, strength=0.6, vol_mult=2.6, ATR=$50):
- `asym = 0.4 × 0.6 = 0.24`
- `buy_mult = 2.6 × 0.76 = 1.98` → `buy_step = $33`
- `sell_mult = 2.6 × 1.24 = 3.22` → `sell_step = $54`
- Buy side is 38% tighter than sell side

### Step Calculation

```python
# Use 1H ATR for step sizing (more robust than stddev for extreme moves)
atr_pct = calc_kline_volatility(candles)  # ATR as % of price
atr_dollar = atr_pct / 100 * current_price

# Directional steps
buy_step  = (buy_mult * atr_dollar) / (GRID_LEVELS / 2)
sell_step = (sell_mult * atr_dollar) / (GRID_LEVELS / 2)

# Clamp both to bounds
buy_step  = clamp(buy_step, price * STEP_MIN_PCT, price * STEP_MAX_PCT)
sell_step = clamp(sell_step, price * STEP_MIN_PCT, price * STEP_MAX_PCT)
buy_step  = max(buy_step, 5.0)   # hard floor: $5
sell_step = max(sell_step, 5.0)
step = (buy_step + sell_step) / 2  # backward-compatible average
```

### Level Construction

`_build_level_prices()` constructs non-uniform grids:

```python
def _build_level_prices(center, buy_step, sell_step, half, grid_type):
    """Below center: spaced by buy_step. Above center: spaced by sell_step."""
    if grid_type == "geometric":
        buy_ratio = 1 + (buy_step / center)
        sell_ratio = 1 + (sell_step / center)
        below = [center / (buy_ratio ** (half - i)) for i in range(half)]
        above = [center * (sell_ratio ** (i + 1)) for i in range(half)]
    else:  # arithmetic
        below = [center - (half - i) * buy_step for i in range(half)]
        above = [center + (i + 1) * sell_step for i in range(half)]
    return below + [center] + above
```

**Symmetric example** (center=$2000, step=$33):
```
[1901, 1934, 1967, 2000, 2033, 2066, 2099]
  ← $33 spacing →         ← $33 spacing →
```

**Asymmetric example** (center=$2000, buy_step=$33, sell_step=$54):
```
[1901, 1934, 1967, 2000, 2054, 2108, 2162]
  ← $33 spacing →         ← $54 spacing →
```

### Level Lookup

```python
import bisect
current_level = bisect.bisect_right(grid["level_prices"], price) - 1
# Clamped to [0, GRID_LEVELS]
```

---

## 4. Grid Recalibration

The grid recalibrates when market conditions shift significantly. Recalibration is **asymmetric** to prevent chasing spikes.

### Trigger Conditions

| Trigger | Condition | Behavior |
|---------|-----------|----------|
| Downside breakout | `price < grid_lower - buy_step` | Recalibrate **immediately** |
| Upside breakout | `price > grid_upper + sell_step` | Require `UPSIDE_CONFIRM_TICKS` (6) consecutive ticks above |
| Volatility shift | `abs(current_kline_atr - grid_atr) / grid_atr > 0.3` | Recalibrate |
| Age | `hours_since_grid_set > GRID_RECALIBRATE_HOURS` (12h) | Recalibrate |

### Anti-Chase Mechanism

For upside breakouts:

1. `upside_breakout_ticks` counter increments each tick price stays above grid
2. If price returns to grid range before reaching threshold, counter **resets to 0**
3. Even after confirmation, center shift is capped:
   ```python
   new_center = clamp(
       calculated_center,
       old_center * (1 - MAX_CENTER_SHIFT_PCT),
       old_center * (1 + MAX_CENTER_SHIFT_PCT)
   )
   ```
4. Multiple recalibrations can gradually track a real trend, but a single spike cannot drag the grid

---

## 5. Trend-Adaptive Position Sizing

### Strategy: `trend_adaptive`

The default sizing strategy adjusts trade amounts based on trend direction:

```
BULLISH trend:
  BUY  -> larger  (accumulate during uptrend)
  SELL -> smaller (preserve position)

BEARISH trend:
  BUY  -> smaller (cautious buying)
  SELL -> larger  (reduce exposure)

NEUTRAL:
  Equal sizing
```

### Multiplier Calculation

```python
def _calc_sizing_multiplier(level, grid_levels, direction, mtf):
    base_mult = 1.0

    if mtf:
        trend = mtf["trend"]
        strength = mtf["strength"]

        if trend == "bullish":
            if direction == "BUY":
                base_mult = 1.0 + strength * (MAX - 1.0)
            else:  # SELL
                base_mult = 1.0 - strength * (1.0 - MIN)
        elif trend == "bearish":
            if direction == "SELL":
                base_mult = 1.0 + strength * (MAX - 1.0)
            else:  # BUY
                base_mult = 1.0 - strength * (1.0 - MIN)

    return clamp(base_mult, SIZING_MULTIPLIER_MIN, SIZING_MULTIPLIER_MAX)
```

### Trade Amount

```python
available_eth = eth_balance - GAS_RESERVE_ETH
total_usd = available_eth * price + usdc_balance
max_usd = total_usd * MAX_TRADE_PCT * multiplier

if direction == "SELL":
    amount = min(max_usd / price, available_eth)
    return int(amount * 1e18)  # wei
else:  # BUY
    amount = min(max_usd, usdc_balance * 0.95)
    return int(amount * 1e6)  # micro-USDC
```

---

## 6. Sell Trailing Optimization

Sell delay is applied in strong uptrends to avoid premature profit-taking.

### Logic Flow

```
SELL signal detected
  |
  v
Is momentum_1h > SELL_MOMENTUM_THRESHOLD (0.5%) AND trend == "bullish"?
  |-- Yes -> SKIP sell ("trend_hold")
  |-- No  -> Check trailing counter
               |
               v
          sell_trail_counter[level_key] < SELL_TRAIL_TICKS (2)?
            |-- Yes -> Increment counter, SKIP ("sell_trail N/2")
            |-- No  -> Counter satisfied, EXECUTE sell
```

The `structure == "uptrend"` condition was removed from momentum protection.
The 8h strict monotonic structure detection was never satisfied in production (always "ranging"),
making the momentum protection dead code. Now only requires bullish trend + strong momentum.

### Key Properties

- **Level-specific**: Each level transition (e.g., "2->3") has its own counter
- **Counter resets**: If price returns to a lower level, the counter for that transition resets
- **Maximum delay**: 2 ticks = 10 minutes at 5-min intervals
- **Momentum override**: Strong momentum (>0.5% in 1h) can block sell indefinitely while bullish trend holds

---

## 7. HODL Alpha Tracking

Measures whether the grid strategy outperforms simple ETH holding.

```python
initial_eth_price = state["stats"]["initial_eth_price"]  # recorded at bot start
initial_portfolio = state["stats"]["initial_portfolio_usd"]

# What HODL would be worth now
initial_eth_amount = initial_portfolio / initial_eth_price
hodl_value = initial_eth_amount * current_price

# Grid alpha
alpha = current_portfolio_usd - hodl_value
```

**Interpretation**:
- `alpha > 0`: Grid is outperforming HODL (good in ranging/declining markets)
- `alpha < 0`: HODL would have been better (expected in strong uptrends)
- In a +9% uptrend backtest, alpha was -5.05% — the trend-adaptive features aim to minimize this gap
