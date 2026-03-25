---
name: grid-trading
description: "Dynamic grid trading strategy for any token pair on EVM L2 chains via OKX DEX API. Features asymmetric grid steps (buy-dense/sell-wide in bullish, reverse in bearish), multi-timeframe trend analysis, trend-adaptive grid sizing, ATR-based volatility, sell trailing optimization, and HODL Alpha tracking. Covers grid modes (arithmetic/geometric), asymmetric buy/sell grid spacing, position sizing strategies (equal/martingale/anti-martingale/pyramid/trend-adaptive), comprehensive risk controls (stop-loss, take-profit, drawdown protection, circuit breakers), trade execution via OKX DEX aggregator, PnL calculation, and Discord notification. Use when creating, modifying, debugging, or tuning a grid trading bot."
license: Apache-2.0
metadata:
  author: SynthThoughts
  version: "1.4.0"
  pattern: "pipeline, tool-wrapper"
  steps: "5"
  openclaw:
    requires:
      env:
        - OKX_API_KEY
        - OKX_SECRET_KEY
        - OKX_PASSPHRASE
        - WALLET_ADDR
      optional_env:
        - ONCHAINOS_ACCOUNT_ID
        - DISCORD_CHANNEL_ID
        - DISCORD_BOT_TOKEN
      bins:
        - onchainos
        - python3
    primaryEnv: OKX_API_KEY
    entrypoint: references/eth_grid_v1.py
    os:
      - darwin
      - linux
---

# Dynamic Grid Trading Strategy v1.0

Cron-driven grid bot for EVM L2 chains via `onchainos` CLI. Features asymmetric grid steps — different spacing for buy vs sell sides based on trend direction, trend intelligence with multi-timeframe analysis, sell optimization, and HODL Alpha tracking.

Every tick: fetch price → MTF analysis → compute grid level → trend-adaptive decision → execute swap → report to Discord.

## Asymmetric Grid Steps

Buy and sell sides use different spacing based on trend direction:

| Trend | Buy Side | Sell Side | Effect |
|-------|----------|-----------|--------|
| Bullish | Tighter (accumulate fast) | Wider (hold longer) | Buy dense, sell sparse → captures uptrend |
| Bearish | Wider (wait for dip) | Tighter (exit fast) | Sell dense, buy sparse → reduces downside exposure |
| Neutral/Weak | Symmetric | Symmetric | Symmetric (default) |

Key config: `ASYM_FACTOR=0.4` (max asymmetry ratio). Asymmetry scales with trend strength and only activates when `strength > 0.3`.

New grid dict fields: `buy_step`, `sell_step` (backward-compatible `step` = average). Level prices are now non-uniform: below center spaced by `buy_step`, above center by `sell_step`.

## Architecture

```
Cron (5min) → Python script → onchainos CLI → OKX Web3 API → Chain
                  ↓                ↓
            state_v1.json    Wallet (TEE signing)
                  ↓
            ┌─────────────┐
            │ MTF Analysis │ ← price_history (288 bars = 24h)
            │ K-line ATR   │ ← onchainos market kline (1H × 24)
            └──────┬──────┘
                   ↓
            Trend-Adaptive Grid Decision
                   ↓
            Discord embed (notification)
```

**OKX Skill Dependencies** (via `onchainos` CLI — handles auth, chain resolution, error retry):
- Price: `okx-dex-market` → `onchainos swap quote --from <A> --to <B> --amount <amt> --chain <chain>`
- K-line: `okx-dex-market` → `onchainos market kline --address <token> --chain <chain> --bar 1H --limit 24`
- Quote: `okx-dex-swap` → `onchainos swap quote --from <A> --to <B> --amount <amt> --chain <chain>`
- Swap: `okx-dex-swap` → `onchainos swap swap --from <A> --to <B> --amount <amt> --chain <chain> --wallet <addr> --slippage <pct>`
- Approve: `okx-dex-swap` → `onchainos swap approve --token <addr> --amount <amt> --chain <chain>`
- Simulate: `okx-onchain-gateway` → `onchainos gateway simulate --from <addr> --to <addr> --data <hex> --chain <chain>`
- Broadcast: `okx-onchain-gateway` → `onchainos gateway broadcast --signed-tx <hex> --address <addr> --chain <chain>`
- Balance: `okx-wallet-portfolio` → `onchainos wallet balance --chain <chain>`

## Pipeline: Execution Steps

**CRITICAL RULE**: Steps MUST execute in order. Do NOT skip steps or proceed past a gate that has not been satisfied.

### Step 1: Data Acquisition

**Actions**:
1. Fetch ETH price via `onchainos swap quote`
2. Fetch on-chain balances via `onchainos wallet balance`
3. Update `price_history` (append, cap at 288 = 24h @ 5min)
4. Detect external deposits/withdrawals (unexplained balance changes > $100)

**Gate** (ALL must pass):
- [ ] Price is non-null and > 0
- [ ] At least one balance (ETH or USDC) > 0
- [ ] Circuit breaker not active (`consecutive_errors < 5`)
- [ ] Stop not triggered (`stop_triggered == null`)

### Step 2: Multi-Timeframe Analysis

**Actions**:
1. Compute short EMA (25min / 5-bar), medium EMA (1h / 12-bar), long EMA (4h / 48-bar)
2. Detect EMA alignment → trend direction (bullish / bearish / neutral) + strength (0-1)
3. Detect 8h structure: split into 4 segments, check higher-highs/higher-lows → uptrend / downtrend / ranging
4. Compute 1h and 4h momentum
5. Fetch K-line data (1H candles, 24 bars) → compute ATR-based volatility (hourly cache)

**Output**: `mtf` dict, `kline_vol` float

```python
def analyze_multi_timeframe(history, price) -> dict:
    # Returns {trend, strength, momentum_1h, momentum_4h, structure,
    #          ema_short, ema_medium, ema_long}
    # EMA alignment: short > medium > long → bullish
    # Structure: 8h window split into 4 segments
    #   higher-highs + higher-lows → "uptrend"
    #   lower-highs + lower-lows → "downtrend"
    #   else → "ranging"
```

**Gate**:
- [ ] `mtf` dict has `trend` and `strength` fields (graceful fallback to neutral if insufficient history)

### Step 3: Grid Decision

**Actions**:
1. Calculate dynamic grid with trend-adaptive volatility multiplier:
   - Grid center = EMA(20) on **1H kline** (20-hour EMA, fetched via `onchainos market kline`). Falls back to 5min tick history if kline unavailable.
   - Base: `VOLATILITY_MULTIPLIER_BASE=2.0`
   - In trend (strength > 0.3): blend toward `VOLATILITY_MULTIPLIER_TREND=3.0`
   - Wider grid in trends → fewer trades → more holding
2. Check recalibration triggers (breakout / vol shift / age)
3. Map price → grid level
4. If level changed: determine direction (BUY if level dropped, SELL if rose)
5. Safety checks: cooldown, trend-adaptive position limits, repeat guard, consecutive limit
6. Sell optimization: if SELL in strong uptrend, delay via `_should_delay_sell()`
7. Calculate trade size with trend-adaptive sizing

**Gate**:
- [ ] Grid is valid (step > 0, levels > 0)
- [ ] If trade needed: amount >= `MIN_TRADE_USD` ($5)
- [ ] All safety checks passed (cooldown, position limits, etc.)

### Step 4: Execution

**Actions**:
1. Get swap quote + tx data from OKX DEX aggregator
2. Pre-simulate via `onchainos gateway simulate` (diagnostic, non-blocking)
3. For BUY: ensure USDC approval for router
4. Sign + broadcast via `onchainos wallet contract-call` (TEE signing)
5. On failure: 1 auto-retry with 3s delay and fresh quote
6. Record trade in state, update grid level ONLY on success

**Gate**:
- [ ] Transaction hash received, or failure recorded with `retriable` flag
- [ ] Level updated only on success; NOT on failure or skip

### Step 5: Notification & Tracking

**Actions**:
1. Calculate PnL (USD + ETH denominated)
2. Calculate HODL Alpha: `current_portfolio - (initial_eth × current_price)`
3. Build structured JSON output for AI agent parsing
4. Send Discord embed (green=SELL, blue=BUY, grey=no-trade, red=stop)
5. Emit `---JSON---` block with enriched fields

**Output**: Discord notification + structured JSON

## Tool Wrapper: onchainos CLI Reference

### Prerequisites

```bash
which onchainos  # must be installed
# Auth via environment variables
OKX_API_KEY=...
OKX_SECRET_KEY=...
OKX_PASSPHRASE=...
```

### Core Operations

| Operation | Command | Input | Output |
|---|---|---|---|
| Get Price | `onchainos swap quote --from $ETH --to $USDC --amount 1e18 --chain base` | Token addresses | `{"toTokenAmount": "2090450000"}` → parse / 1e6 |
| Get K-line | `onchainos market kline --address $ETH --chain base --bar 1H --limit 24` | Token, bar size, limit | `[{ts, open, high, low, close, volume}]` |
| Get Balance | `onchainos wallet balance --chain 8453` | Chain ID | `{details: [{tokenAssets: [{symbol, balance}]}]}` |
| Swap Quote+TX | `onchainos swap swap --from $A --to $B --amount $amt --chain base --wallet $addr --slippage 1` | Tokens, amount, wallet | `{data: [{tx: {to, data, value, gas}}]}` |
| Approve ERC-20 | `onchainos swap approve --token $USDC --amount $max --chain base` | Token, amount | Approval TX data |
| Simulate TX | `onchainos gateway simulate --from $wallet --to $to --data $hex --chain base` | TX params | `{failReason, gasUsed}` |
| Sign+Broadcast | `onchainos wallet contract-call --to $addr --chain 8453 --input-data $hex --value $eth` | TX params | `{txHash}` |

### Error Handling Protocol

Every function returns `(result, failure_info)`. Failure info is structured:

```python
failure_info = {
    "reason": str,      # machine-readable: "swap_quote_failed", "approval_failed", etc.
    "detail": str,      # human-readable context
    "retriable": bool,  # safe to auto-retry?
    "hint": str         # "transient_api_error", "retry_with_fresh_quote", "low_balance"
}
```

Auto-retry policy: 1 retry for `retriable=True` with 3s delay and fresh quote.

## Tunable Parameters

### Grid Structure

| Parameter | Default | Description |
|---|---|---|
| `GRID_LEVELS` | `6` | Number of grid levels. More = finer, more trades |
| `GRID_TYPE` | `"arithmetic"` | `"arithmetic"` (fixed $ step) or `"geometric"` (fixed % step) |
| `EMA_PERIOD` | `20` | EMA lookback for grid center (applied to 1H kline = 20h) |
| `VOLATILITY_MULTIPLIER_BASE` | `2.0` | Base grid width = multiplier x stddev |
| `VOLATILITY_MULTIPLIER_TREND` | `3.0` | Wider grid in trending markets |
| `ASYM_FACTOR` | `0.4` | Max buy/sell asymmetry ratio. 0=symmetric, 1=fully asymmetric |
| `GRID_RECALIBRATE_HOURS` | `12` | Max hours before forced recalibration |

### Multi-Timeframe

| Parameter | Default | Description |
|---|---|---|
| `MTF_SHORT_PERIOD` | `5` | 5-bar EMA (25min @ 5min tick) |
| `MTF_MEDIUM_PERIOD` | `12` | 12-bar EMA (1h @ 5min tick) |
| `MTF_LONG_PERIOD` | `48` | 48-bar EMA (4h @ 5min tick) |
| `MTF_STRUCTURE_PERIOD` | `96` | 96-bar (8h @ 5min tick) for structure detection |

### Sell Optimization

| Parameter | Default | Description |
|---|---|---|
| `SELL_TRAIL_TICKS` | `2` | Wait 2 ticks (10min) of price stability before selling in uptrend |
| `SELL_MOMENTUM_THRESHOLD` | `0.005` | Skip sell if 1h momentum > 0.5% in strong uptrend |

### Grid Modes

**Arithmetic (等差网格)**: Each level is a fixed USD distance apart. Good for narrow ranges.

```
levels = [center - N*step, ..., center, ..., center + N*step]
```

**Geometric (等比网格)**: Each level is a fixed percentage apart. Better for wide ranges because step size scales with price. The ratio is derived from the arithmetic step: `ratio = 1 + (step / center)`.

```python
# In calc_dynamic_grid(), when GRID_TYPE == "geometric":
ratio = 1 + (step / center)
level_prices = [center * (ratio ** (i - half)) for i in range(GRID_LEVELS + 1)]
```

Both modes store `level_prices` in the grid dict for unified level lookup via `bisect_right`. Asymmetric spacing uses levels below center spaced by `buy_step`, levels above by `sell_step`. The `_build_level_prices()` helper handles both symmetric and asymmetric construction.

**Choosing a mode:**

| Market | Recommended | Why |
|---|---|---|
| Tight range ($1900-$2100) | Arithmetic | Even spacing, predictable profit per grid |
| Wide range ($1500-$3000) | Geometric | Steps scale with price, avoids crowding at low end |
| High volatility | Geometric | Naturally wider steps at higher prices |
| Stablecoin pairs | Arithmetic | Fixed small steps (0.1-0.5%) |

### Adaptive Step Sizing

Step scales with real-time volatility, modulated by trend strength. Splits into directional buy/sell steps:

```
vol_mult = VOLATILITY_MULTIPLIER_BASE  (2.0)
if trend_strength > 0.3:
    vol_mult = blend(BASE, TREND, strength)  (up to 3.0)

# Asymmetric steps based on trend direction
asym = ASYM_FACTOR × strength  (if strength > 0.3, else 0)
if bullish:
    buy_mult  = vol_mult × (1 - asym)   # tighter buy
    sell_mult = vol_mult × (1 + asym)   # wider sell
elif bearish:
    buy_mult  = vol_mult × (1 + asym)   # wider buy
    sell_mult = vol_mult × (1 - asym)   # tighter sell

buy_step  = (buy_mult × ATR) / (GRID_LEVELS / 2)
sell_step = (sell_mult × ATR) / (GRID_LEVELS / 2)
# Both clamped to [price × STEP_MIN_PCT, price × STEP_MAX_PCT], floor $5
step = (buy_step + sell_step) / 2  # backward-compatible average
```

| Parameter | Default | Description |
|---|---|---|
| `STEP_MIN_PCT` | `0.010` | Step floor as fraction of price (1.0%) |
| `STEP_MAX_PCT` | `0.060` | Step cap as fraction of price (6%) |
| `VOL_RECALIBRATE_RATIO` | `0.3` | Recalibrate if kline ATR shifts >30% from grid's stored ATR |

**Recalibration triggers (asymmetric):**
1. **Downside breakout**: Price < grid lower - `buy_step` → recalibrate **immediately** (buying dips is grid's edge)
2. **Upside breakout**: Price > grid upper + `sell_step` → require **N consecutive ticks** confirmation before recalibrating (anti-chase)
3. Grid age exceeds `GRID_RECALIBRATE_HOURS`
4. Current kline ATR deviates >30% from grid's stored ATR

| Parameter | Default | Description |
|---|---|---|
| `UPSIDE_CONFIRM_TICKS` | `6` | Ticks (30min @ 5min interval) price must hold above grid before upside recalibration |
| `MAX_CENTER_SHIFT_PCT` | `0.03` | Max 3% grid center shift per recalibration (prevents chasing spikes) |

**Anti-chase mechanism:**
- Upside breakout counter resets if price returns to grid range before confirmation
- Even after confirmation, center shifts are capped to `MAX_CENTER_SHIFT_PCT` per recalibration
- Multiple recalibrations can gradually track a true trend, but a single spike cannot drag the grid

### Position Sizing Strategies

Controls how much to trade at each grid level.

| Strategy | Description |
|---|---|
| `"equal"` | Every grid level trades the same amount |
| `"martingale"` | BUY more at lower levels, SELL more at higher levels |
| `"anti_martingale"` | Reduces exposure as price moves further from center |
| `"pyramid"` | Largest position at grid center, tapering toward edges |
| `"trend_adaptive"` | **(default)** In bullish: buy more + sell less. In bearish: sell more + buy less |

```python
def _calc_sizing_multiplier(level, grid_levels, direction, mtf=None):
    base_mult = 1.0
    if SIZING_STRATEGY == "trend_adaptive" and mtf:
        trend = mtf.get("trend", "neutral")
        strength = mtf.get("strength", 0)
        if trend == "bullish":
            if direction == "BUY":
                base_mult = 1.0 + strength * (MAX - 1.0)   # buy aggressively
            else:
                base_mult = 1.0 - strength * (1.0 - MIN)   # sell conservatively
        elif trend == "bearish":
            # opposite
            ...
    return clamp(base_mult, SIZING_MULTIPLIER_MIN, SIZING_MULTIPLIER_MAX)
```

### Trade Execution

| Parameter | Default | Description |
|---|---|---|
| `MAX_TRADE_PCT` | `0.12` | Max 12% of portfolio per trade (before sizing multiplier) |
| `MIN_TRADE_USD` | `5.0` | Minimum trade size in USD |
| `SLIPPAGE_PCT` | `1` | Slippage tolerance for DEX swap |
| `GAS_RESERVE` | `0.003` | Native token reserved for gas |

### Risk Controls

#### Basic Controls

| Parameter | Default | Description |
|---|---|---|
| `MIN_TRADE_INTERVAL` | `1800` | 30min cooldown between same-direction trades |
| `MAX_SAME_DIR_TRADES` | `3` | Max consecutive same-direction trades |
| `MAX_CONSECUTIVE_ERRORS` | `5` | Circuit breaker threshold |
| `COOLDOWN_AFTER_ERRORS` | `3600` | Cooldown after circuit breaker trips |
| `POSITION_MAX_PCT_DEFAULT` | `70` | Block BUY when ETH > this % |
| `POSITION_MIN_PCT_DEFAULT` | `30` | Block SELL when ETH < this % |
| `POSITION_MAX_PCT_BULLISH` | `80` | Allow more ETH in bullish trend |
| `POSITION_MIN_PCT_BEARISH` | `25` | Allow less ETH in bearish trend |

Additional safety guards:
- **Rapid drop protection**: skip BUY if price dropped >2% in last 30min (6 ticks)
- **Consecutive same-direction reset**: limit resets if grid was recalibrated or >1h since last trade
- **Anti-repeat**: skip if same direction + same level boundary as last trade
- **Trend-adaptive position limits**: limits shift based on trend direction and strength

#### Stop-Loss, Trailing Stop & Take-Profit

Three protection mechanisms. When triggered, trading halts and a red Discord alert is sent. Use `resume-trading` command to clear.

| Parameter | Default | Description |
|---|---|---|
| `STOP_LOSS_PCT` | `0.15` | Stop if portfolio drops 15% below cost basis |
| `TRAILING_STOP_PCT` | `0.10` | Stop if portfolio drops 10% from peak |

```python
def _check_stop_conditions(state, total_usd, price):
    cost_basis = initial + deposits
    pnl_pct = (total_usd - cost_basis) / cost_basis
    peak = max(stats["portfolio_peak_usd"], total_usd)
    # Check: stop_loss, trailing_stop, take_profit
```

#### Risk Control Flow in tick()

```
1. If stop_triggered is set → log + Discord red alert + refuse trading + return
2. Check _check_stop_conditions → if triggered, set stop_triggered + alert + return
3. Multi-timeframe analysis → get trend context
4. Normal grid logic (cooldown, trend-adaptive position limits, etc.)
5. If SELL in strong uptrend → _should_delay_sell() check
```

## Core Algorithm

```
1. Fetch token price
2. Read on-chain balances (ETH + USDC)
3. Multi-timeframe analysis → trend/strength/momentum/structure
4. Fetch K-line ATR volatility (hourly cache)
5. Check if grid needs recalibration (breakout / vol shift / age)
   → calc_dynamic_grid() uses trend-adaptive volatility multiplier
   → asymmetric buy_step/sell_step based on trend direction
6. Map price → grid level
7. If level changed:
   a. Direction: BUY if level dropped, SELL if rose
   b. If SELL in strong uptrend → delay check (trailing + momentum protection)
   c. Safety checks (cooldown, trend-adaptive position limits, repeat guard, consecutive limit)
   d. Calculate trade size (trend-adaptive sizing)
   e. Execute swap via DEX aggregator
   f. Record trade, update level ONLY on success
8. Calculate HODL Alpha
9. Report status (JSON + Discord)
```

## Grid Calculation

```python
def calc_dynamic_grid(price, price_history, mtf=None):
    center = EMA(1H_kline, EMA_PERIOD)  # 20-hour EMA on 1H candles
    atr = calc_kline_volatility(candles)

    # Trend-adaptive multiplier
    vol_mult = VOLATILITY_MULTIPLIER_BASE  # 2.0
    if mtf and mtf["strength"] > 0.3:
        vol_mult = blend(BASE=2.0, TREND=3.0, factor=strength)

    # Asymmetric buy/sell multipliers
    asym = ASYM_FACTOR * strength if strength > 0.3 else 0
    if bullish:
        buy_mult = vol_mult * (1 - asym)   # tighter buy grid
        sell_mult = vol_mult * (1 + asym)   # wider sell grid
    elif bearish:
        buy_mult = vol_mult * (1 + asym)   # wider buy grid
        sell_mult = vol_mult * (1 - asym)   # tighter sell grid

    buy_step = clamp((buy_mult * atr) / half, floor, ceil)
    sell_step = clamp((sell_mult * atr) / half, floor, ceil)

    # Build asymmetric level_prices via _build_level_prices()
    # Below center: spaced by buy_step; Above center: spaced by sell_step
    level_prices = _build_level_prices(center, buy_step, sell_step, half, grid_type)

    return {center, step, buy_step, sell_step, levels, range, vol_pct, type, level_prices}
```

**Examples** (at price $2000, ATR=$50):

| Trend | Strength | buy_step | sell_step | Buy Range | Sell Range | Behavior |
|---|---|---|---|---|---|---|
| Neutral | 0.1 | $33 | $33 | $1901-$2000 | $2000-$2099 | Symmetric, normal |
| Bullish | 0.6 | $33 | $54 | $1901-$2000 | $2000-$2162 | Buy dense + sell wide |
| Bearish | 0.6 | $54 | $33 | $1838-$2000 | $2000-$2099 | Buy wide + sell dense |
| Strong Bull | 0.9 | $24 | $66 | $1928-$2000 | $2000-$2198 | Max asymmetry |

## Sell Optimization Logic

```python
def _should_delay_sell(state, current_level, prev_level, mtf, history):
    """Returns skip reason or None."""

    # 1. Momentum protection: skip sell if 1h momentum > 0.5% in uptrend
    if momentum_1h > SELL_MOMENTUM_THRESHOLD * 100:
        if trend == "bullish" and structure == "uptrend":
            return "trend_hold (momentum +X.X%)"

    # 2. Trailing sell: wait SELL_TRAIL_TICKS (2) before executing
    trail = state["sell_trail_counter"]
    level_key = f"{prev_level}->{current_level}"
    if trail[level_key] < SELL_TRAIL_TICKS:
        trail[level_key] += 1
        return "sell_trail (N/2)"

    # 3. Cleared → proceed with sell
    return None
```

## Trend-Adaptive Position Limits

```python
def _get_position_limits(mtf):
    """Return (max_pct, min_pct) based on trend."""
    if trend == "bullish" and strength > 0.3:
        max_pct = 70 + (80 - 70) * strength  # allow more ETH
        min_pct = 30
    elif trend == "bearish" and strength > 0.3:
        max_pct = 70
        min_pct = 30 - (30 - 25) * strength  # allow less ETH
    else:
        max_pct, min_pct = 70, 30
```

## Trade Size

Returns `(amount_in_smallest_unit, failure_info)`. SELL returns wei (x1e18), BUY returns uUSDC (x1e6).

```python
def calc_trade_amount(direction, eth_bal, usdc_bal, price,
                      current_level=None, grid_levels=None,
                      mtf=None):
    available_eth = eth_bal - GAS_RESERVE_ETH
    total_usd = available_eth * price + usdc_bal
    max_usd = total_usd * MAX_TRADE_PCT

    # Apply trend-adaptive sizing
    multiplier = _calc_sizing_multiplier(level, grid_levels, direction, mtf)
    max_usd *= multiplier

    if direction == "SELL":
        return int(min(max_usd / price, available_eth) * 1e18), None  # wei
    else:
        return int(min(max_usd, usdc_bal * 0.95) * 1e6), None  # uUSDC
```

## Level Update Rule (Critical)

| Outcome | Update level? | Rationale |
|---|---|---|
| Trade succeeded | Yes | Grid crossing consumed |
| Trade failed | No | Retry on next tick |
| Trade skipped (cooldown/limit) | No | Don't lose the crossing |
| Sell delayed (trailing/momentum) | No | Will retry next tick |

## PnL Tracking (dual-denominated)

```
# USD-denominated
total_pnl_usd = current_portfolio_usd - cost_basis
hodl_value_usd = initial_eth_amount × current_price
grid_alpha_usd = current_portfolio_usd - hodl_value_usd

# ETH-denominated
current_eth_equivalent = current_portfolio_usd / current_price
initial_eth_equivalent = cost_basis / initial_price
total_pnl_eth = current_eth_equivalent - initial_eth_equivalent
```

**HODL Alpha** (key metric): `grid_alpha_usd > 0` means grid outperforms pure ETH holding.

## State Schema

```json
{
  "version": 5,
  "grid": {"center": 2000, "step": 43.5, "buy_step": 33.3, "sell_step": 53.7,
           "levels": 6, "range": [1900, 2161], "vol_pct": 2.1,
           "type": "arithmetic",
           "level_prices": [1900, 1933, 1967, 2000, 2054, 2107, 2161]},
  "grid_set_at": "ISO timestamp",
  "current_level": 3,
  "price_history": ["...max 288 (24h at 5min)"],
  "trades": [{"time": "...", "direction": "SELL", "price": 2050,
              "amount_usd": 25, "est_profit": 1.5, "tx": "0x...",
              "grid_from": 2, "grid_to": 3}],
  "stats": {
    "total_trades": 15,
    "realized_pnl": 5.2,
    "grid_profit": 3.8,
    "initial_portfolio_usd": 1000,
    "initial_eth_price": 2000.0,
    "portfolio_peak_usd": 1050.0,
    "total_deposits_usd": 0.0,
    "deposit_history": [],
    "trade_attempts": 10, "trade_successes": 10, "trade_failures": 0,
    "sell_attempts": 5, "sell_successes": 5,
    "buy_attempts": 5, "buy_successes": 5,
    "retry_attempts": 0, "retry_successes": 0,
    "started_at": "ISO timestamp",
    "last_check": "ISO timestamp"
  },
  "stop_triggered": null,
  "last_trade_times": {"BUY": "...", "SELL": "..."},
  "last_failed_trade": null,
  "last_balances": {"eth": 0.134, "usdc": 257.33, "time": "ISO timestamp"},
  "last_quiet_report": "ISO timestamp",
  "upside_breakout_ticks": 0,
  "approved_routers": ["0x..."],
  "errors": {"consecutive": 0, "cooldown_until": null},
  "mtf_cache": null,
  "kline_cache": null,
  "sell_trail_counter": {}
}
```

Key fields:
- `grid.type` + `grid.level_prices`: geometric/arithmetic grid support (asymmetric spacing)
- `grid.buy_step` / `grid.sell_step`: directional step sizes; `grid.step` = average for backward compat
- `stats.initial_eth_price`: records ETH price at bot start for HODL Alpha calculation
- `stats.portfolio_peak_usd`: highest portfolio value (for trailing stop)
- `stop_triggered`: string describing trigger condition, or null
- `last_failed_trade`: cached for `retry` command (expires after 10min)
- `upside_breakout_ticks`: confirmation counter for upside recalibration
- `approved_routers`: USDC approval cache to avoid redundant approvals
- `mtf_cache`: cached multi-timeframe analysis result
- `kline_cache`: cached K-line data (1h TTL)
- `sell_trail_counter`: tracks sell delay tick counts per level transition

## Operational Interface

### Sub-Commands

| Command | Purpose | Trigger |
|---|---|---|
| `tick` | Main loop: price → MTF → grid → trade → report | Cron every 5min |
| `status` | Print current grid state, balances, PnL, trend | On demand |
| `report` | Generate daily performance report (Chinese) | Cron daily 08:00 CST |
| `history` | Show recent trade history | On demand |
| `reset` | Reset grid (recalibrate from scratch), keep trade history | Manual |
| `retry` | Retry last failed trade with fresh quote (expires after 10min) | AI agent / manual |
| `analyze` | Output detailed market + MTF + round-trip analysis JSON | AI agent |
| `deposit` | Manually record deposit/withdrawal for PnL tracking | Manual |
| `resume-trading` | Clear stop_triggered flag and resume trading | Manual / AI agent |

```python
COMMANDS = {
    "tick": tick, "status": status, "report": report,
    "history": history_cmd, "reset": reset, "retry": retry,
    "analyze": analyze, "deposit": deposit,
    "resume-trading": resume_trading
}
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    COMMANDS.get(cmd, tick)()
```

### AI Agent Output Protocol

The `tick` command outputs a structured JSON block for AI agent parsing:

```
---JSON---
{
  "version": "1.0",
  "status": "trade_executed" | "no_trade" | "cooldown" | "trade_failed" | ...,
  "market": {
    "price": 2090.45, "ema": 2085.3, "volatility_pct": 1.2,
    "trend": "bullish", "trend_strength": 0.65,
    "momentum_1h": 0.35, "momentum_4h": 1.2,
    "structure": "uptrend",
    "kline_atr_pct": 1.8
  },
  "portfolio": {"eth": 0.134, "usdc": 257.33, "total_usd": 538.0, "eth_pct": 52.1},
  "grid_level": 3,
  "direction": "SELL",
  "tx_hash": "0x...",
  "failure_reason": "...",
  "retriable": true,
  "hodl_alpha": 2.15,
  "success_rate": {"total_attempts": 182, "successes": 182, "rate_pct": 100.0}
}
```

The `analyze` command outputs additional fields:
- `multi_timeframe`: full MTF data (EMA short/medium/long, momentum, structure)
- `round_trips`: trade pair analysis (good / micro / loss classification)

### Discord Notification

Two card formats pushed via Discord Bot API:

**Trade executed** (colored embed):
- Green = SELL, Blue = BUY
- Fields: price, level, total value, position, PnL, grid profit, HODL Alpha, BaseScan link

**No trade** (grey compact card):
- One-line: price . level . position . PnL . trend . trade count
- Only sent once per `QUIET_INTERVAL` (default 1 hour)

### Deposit/Withdrawal Detection

Automatically detects external balance changes:

```
unexplained_change = (current_balance - last_balance) - sum(recorded_trades_since_last)
if abs(unexplained_change) > $100:
    record as deposit or withdrawal → adjust PnL cost basis
```

### Logging

- File: `grid_bot_v1.log` in script directory
- Rotation: simple half-file rotation at 1MB
- Format: `[YYYY-MM-DD HH:MM:SS] message`

## Backtest Results Summary

Based on 10-day backtest (2026-03-06 to 2026-03-16, ETH +9.00%):

### Best Configuration: 6 Levels + trend_adaptive

| Metric | Value |
|---|---|
| Initial Capital | $511.44 |
| Final Value | $513.66 |
| Total Return | +0.43% |
| Annualized Return | +17.13% |
| **Sharpe Ratio** | **4.45** |
| Sortino Ratio | 4.04 |
| Max Drawdown | 11.92% ($61.72) |
| Total Trades | 54 |
| Total Costs | $42.09 (1.32%) |

### HODL Comparison

| Strategy | Return | Final Value |
|---|---|---|
| Grid | +0.43% | $513.66 |
| HODL | +5.48% | $539.47 |
| Alpha | -5.05% | -$25.81 |

Note: In a +9% uptrend, grid strategies naturally underperform HODL because they sell into the rise. The trend-adaptive features (wider grid, sell delay, momentum protection) aim to reduce this gap.

### Parameter Scan Key Findings

| Finding | Detail |
|---|---|
| `trend_adaptive` > `equal` | +1.8% better return across all grid level configs |
| 6 levels optimal | Best Sharpe (4.45) vs 4 levels (2.05) or 8 levels (4.46 but -0.84% return) |
| `step_min_pct` insensitive | 0.006-0.012 gave identical results at 6 levels — vol floor binding |
| 8 levels + 0.012 step | Sharpe 9.63 but -0.03% return — fewer, higher-quality trades |
| Cost impact | ~1.3% of traded volume — critical to maintain step > 3x DEX cost |

## Adapting to Different Pairs

| Consideration | What to adjust |
|---|---|
| Token decimals | USDC=6, DAI=18, WBTC=8 — affects amount conversion |
| Typical volatility | BTC lower vol → smaller `STEP_MIN/MAX_PCT`; meme coins → larger |
| Liquidity depth | Low liquidity → smaller `MAX_TRADE_PCT`, add price impact check |
| Gas costs | L1 vs L2: adjust `GAS_RESERVE` and `MIN_TRADE_USD` |
| Stablecoin pair | TOKEN/USDC pair: `STEP_MIN_PCT` can be much tighter (0.2%) |
| Rate limits | Add 300-500ms delay between consecutive OKX API calls |

## AI Review & Optimization

AI agent should periodically review trading performance and suggest/apply optimizations. Run weekly or when cumulative PnL stalls.

### Step 1: Pull & Pair Trades

Extract recent trades and pair each BUY with its corresponding SELL to form **round trips**.

```python
# Matching logic: SELL from level A→B matches BUY from level B→A
buy_stack = []
round_trips = []
for trade in trades:
    if trade["direction"] == "BUY":
        buy_stack.append(trade)
    else:  # SELL
        for j in range(len(buy_stack)-1, -1, -1):
            if buy_stack[j]["grid_to"] == trade["grid_from"]:
                matched_buy = buy_stack.pop(j)
                round_trips.append((matched_buy, trade))
                break
```

Output per round trip:
- **Spread**: `(sell_price - buy_price) / buy_price x 100%`
- **Hold time**: minutes between buy and sell
- **Status**: profit (spread > 0.3%), micro-profit (0 < spread < 0.3%), loss (spread < 0)

### Step 2: Flag Anomalies

| Flag | Condition | Meaning |
|---|---|---|
| `LOSS` | spread < 0 | Bought high, sold low |
| `MICRO` | 0 < spread < 0.3% | Profit too small to cover DEX costs |
| `GOOD` | spread >= 0.3% | Healthy grid profit |

Key metrics: win rate, loss impact, micro-trade ratio (if > 30%, step too small).

### Step 3: Root Cause Analysis

**LOSS trades:**

| Pattern | Root Cause | Fix |
|---|---|---|
| Buy @high, sell @low after recalibration | Grid chased a spike | Increase `UPSIDE_CONFIRM_TICKS`, reduce `MAX_CENTER_SHIFT_PCT` |
| Buy @high in trend, sell @low on reversal | EMA too reactive | Increase `EMA_PERIOD` or `GRID_RECALIBRATE_HOURS` |
| Loss during flash crash | Stop-loss too loose | Tighten `STOP_LOSS_PCT` |

**MICRO trades:**

| Pattern | Root Cause | Fix |
|---|---|---|
| Many trades with < 0.2% spread | Step too small | Increase `STEP_MIN_PCT` |
| Rapid back-and-forth at same levels | Low vol, grid too dense | Increase `MIN_TRADE_INTERVAL` |
| Trades cluster in 5-10 min windows | Cooldown too short | Increase `MIN_TRADE_INTERVAL` |

### Step 4: Parameter Tuning

```
STEP_MIN_PCT >= DEX_total_cost x 3
  where DEX_total_cost ~ slippage + price_impact ~ 0.1-0.3% on L2
  → STEP_MIN_PCT >= 0.009 to 0.012

UPSIDE_CONFIRM_TICKS = typical_spike_duration / tick_interval
  e.g., spikes last ~20min, tick=5min → confirm_ticks = 4-6

MAX_CENTER_SHIFT_PCT = step_pct x 2-3
```

### Step 5: Backtest & Apply

Simulate new parameters against historical data, then: backup → patch → recalibrate → monitor 24h → re-run analysis.

### Review Checklist (AI Agent Prompt)

```
1. Read grid_state_v1.json and grid_bot_v1.log
2. Filter trades to review window (default: last 48h)
3. Pair trades into round trips
4. Compute: win_rate, avg_spread, loss_count, micro_count, total_pnl, hodl_alpha
5. If loss_count > 0: trace each loss to recalibration events
6. If micro_ratio > 30%: recommend STEP_MIN_PCT increase
7. Check MTF data for trend alignment during losses
8. Propose specific parameter changes with backtest evidence
9. On user approval: backup → patch → recalibrate → verify
```

## Failure & Rollback

```
IF Step N fails:
  1. Log failure reason to grid_bot_v1.log
  2. Increment errors.consecutive
  3. If errors.consecutive >= 5: trigger circuit breaker (1h cooldown)
  4. Cache failed trade for retry command (10min expiry)
  5. DO NOT update grid level
  6. Report failure via Discord + JSON output
```

## Anti-Patterns

| Pattern | Problem |
|---|---|
| Recalibrate every tick | Grid oscillates, no stable levels |
| Update level on failure/skip | Silently loses grid crossings |
| No position limits | Trending market → 100% one-sided |
| Fixed step in volatile market | Too small → over-trades; too large → never triggers |
| `sell - buy` as PnL | Net cash flow ≠ profit |
| No cooldown | Rapid swings cause burst of trades eating slippage |
| No stop-loss | Single crash wipes out months of grid profits |
| Martingale without cap | Exponential position growth → liquidation risk |
| Arithmetic grid on wide range | $20 step meaningless at $5000 but huge at $500 |
| Symmetric recalibration | Chasing upside spikes = buying high then selling low on reversal |
| Step floor too low | Micro-profit trades only feed DEX fees, net negative after costs |
| No center shift cap | Single spike can drag grid center 5%+, creating losing positions |
| Fixed sizing in trends | Selling same size in uptrend = giving away alpha to the market |
| Selling immediately in uptrend | Sell delay exists for a reason — let trends play out |
| Symmetric grid in strong trends | Asymmetric grids accumulate faster on the favorable side |
| Ignoring `buy_step`/`sell_step` in profit calc | Use actual `level_prices` differences, not average `step` |
