# Grid Algorithm Reference

Detailed explanation of the core algorithms in Grid Trading v4.1.

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
# v4.1: Prefer 1H kline for grid center (more robust than 5min ticks)
candles = get_kline_data(bar="1H", limit=EMA_PERIOD)  # 20 hourly candles
if candles:
    center = EMA([c.close for c in candles], EMA_PERIOD)  # 20-hour EMA
else:
    center = EMA(price_history, EMA_PERIOD)  # fallback: 5min tick history
```

**v4.1 change**: Grid center now uses 1H kline EMA (20h lookback) instead of 5min tick EMA (100min lookback). This produces a more stable center that doesn't drift on short-term noise, better matching the 12-hour recalibration cycle.

### Trend-Adaptive Volatility Multiplier

```python
vol_mult = VOLATILITY_MULTIPLIER_BASE  # 2.0

if mtf and mtf["strength"] > 0.3:
    blend_factor = (mtf["strength"] - 0.3) / 0.7  # normalize 0.3-1.0 to 0-1
    vol_mult = BASE + (TREND - BASE) * blend_factor
    # Range: 2.0 to 3.0
```

**Effect**: In strong trends, the grid becomes wider -> fewer trades -> bot holds position longer -> captures trend moves instead of selling too early.

### Step Calculation

```python
# v4.1: Use 1H ATR for step sizing (more robust than stddev for extreme moves)
atr_pct = calc_kline_volatility(candles)  # ATR as % of price
atr_dollar = atr_pct / 100 * current_price
step = (vol_mult * atr_dollar) / (GRID_LEVELS / 2)

# Clamp to bounds
step = max(step, price * STEP_MIN_PCT)  # floor: 1.0% of price
step = min(step, price * STEP_MAX_PCT)  # cap: 6.0% of price
step = max(step, 5.0)                   # hard floor: $5
```

### Level Construction

**Arithmetic (equal dollar spacing)**:
```python
half = GRID_LEVELS // 2
level_prices = [center - half * step + i * step for i in range(GRID_LEVELS + 1)]
# Example at center=$2000, step=$33: [1901, 1934, 1967, 2000, 2033, 2066, 2099]
```

**Geometric (equal percentage spacing)**:
```python
ratio = 1 + (step / center)
level_prices = [center * (ratio ** (i - half)) for i in range(GRID_LEVELS + 1)]
# Each level is `ratio` times the previous
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
| Downside breakout | `price < grid_lower - step` | Recalibrate **immediately** |
| Upside breakout | `price > grid_upper + step` | Require `UPSIDE_CONFIRM_TICKS` (6) consecutive ticks above |
| Volatility shift | `abs(current_vol - grid_vol) / grid_vol > 0.3` | Recalibrate |
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

The default v4 sizing strategy adjusts trade amounts based on trend direction:

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
def _calc_sizing_multiplier(level, grid_levels, direction, mtf, signal):
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

    # Signal boost
    if signal and signal["bullish_score"] > 0.3:
        if direction == "BUY":
            base_mult *= 1.0 + SIGNAL_WEIGHT * bullish_score
        else:
            base_mult *= 1.0 - SIGNAL_WEIGHT * bullish_score * 0.5

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

v4 introduces sell delay in strong uptrends to avoid premature profit-taking.

### Logic Flow

```
SELL signal detected
  |
  v
Is trend bullish AND structure == "uptrend"?
  |-- No  -> Execute sell immediately
  |-- Yes -> Check momentum protection
              |
              v
         momentum_1h > SELL_MOMENTUM_THRESHOLD (0.5%)?
           |-- Yes -> SKIP sell ("trend_hold")
           |-- No  -> Check trailing counter
                        |
                        v
                   sell_trail_counter[level_key] < SELL_TRAIL_TICKS (2)?
                     |-- Yes -> Increment counter, SKIP ("sell_trail N/2")
                     |-- No  -> Counter satisfied, EXECUTE sell
```

### Key Properties

- **Level-specific**: Each level transition (e.g., "2->3") has its own counter
- **Counter resets**: If price returns to a lower level, the counter for that transition resets
- **Maximum delay**: 2 ticks = 10 minutes at 5-min intervals
- **Momentum override**: Strong momentum (>0.5% in 1h) can block sell indefinitely while trend holds

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
- In a +9% uptrend backtest, alpha was -5.05% — the v4 trend-adaptive features aim to minimize this gap
