---
name: grid-trading
description: "Dynamic grid trading strategy for any token pair on EVM L2 chains via OKX DEX API. Covers grid parameter design, volatility-adaptive step sizing, trade execution via OKX DEX aggregator, position management, risk controls (circuit breakers, cooldowns, position limits), PnL calculation, and Discord notification. Use when creating, modifying, debugging, or tuning a grid trading bot."
license: Apache-2.0
metadata:
  author: SynthThoughts
  version: "1.0.0"
---

# Dynamic Grid Trading Strategy

Cron-driven grid bot for EVM L2 chains via OKX DEX API. Every tick: fetch price → compute grid level → execute swap if level crossed → report to Discord.

## Architecture

```
Cron (5min) → Python script → OKX DEX API (price + swap) + Wallet (signing) → Chain
                    ↓
              state.json (persistent state)
                    ↓
              Discord embed (notification)
```

**OKX Skill Dependencies** (auth/签名/参数详见各 skill):
- Price: `okx-dex-market` → `POST /api/v6/dex/market/price`
- Swap: `okx-dex-swap` → `GET /aggregator/quote` + `/swap` + `/approve-transaction`
- Broadcast: `okx-onchain-gateway` → `POST /pre-transaction/broadcast-transaction`
- Balance: `okx-wallet-portfolio` → `GET /balance/all-token-balances-by-address`

## Core Algorithm

```
1. Fetch token price
2. Read on-chain balances (TOKEN_A + TOKEN_B)
3. Check if grid needs recalibration (price breakout / vol shift / age)
4. Map price → grid level
5. If level changed:
   a. Direction: BUY if level dropped, SELL if rose
   b. Safety checks (cooldown, position limits, repeat guard, consecutive limit)
   c. Calculate trade size (% of portfolio, capped)
   d. Execute swap via DEX aggregator
   e. Record trade, update level ONLY on success
6. Report status
```

## Tunable Parameters

### Grid Structure

| Parameter | Default | Description |
|---|---|---|
| `GRID_LEVELS` | `6` | Number of grid levels. More = finer, more trades |
| `EMA_PERIOD` | `20` | EMA lookback for grid center |
| `VOLATILITY_MULTIPLIER` | `2.5` | Grid width = multiplier × stddev |
| `GRID_RECALIBRATE_HOURS` | `12` | Max hours before forced recalibration |

### Adaptive Step Sizing

Step scales linearly with real-time volatility:

```
step = (VOLATILITY_MULTIPLIER × stddev) / (GRID_LEVELS / 2)
step = clamp(step, price × STEP_MIN_PCT, price × STEP_MAX_PCT)
step = max(step, STEP_FLOOR)
```

| Parameter | Default | Description |
|---|---|---|
| `STEP_MIN_PCT` | `0.008` | Step floor as fraction of price (0.8%) |
| `STEP_MAX_PCT` | `0.060` | Step cap as fraction of price (6%) |
| `STEP_FLOOR` | `5.0` | Absolute minimum step in USD |
| `VOL_RECALIBRATE_RATIO` | `0.3` | Recalibrate if vol shifts >30% from grid snapshot |

**Recalibration triggers:**
1. Price exits grid range by >1 step
2. Grid age exceeds `GRID_RECALIBRATE_HOURS`
3. Current volatility deviates >30% from grid's recorded volatility

### Trade Execution

| Parameter | Default | Description |
|---|---|---|
| `MAX_TRADE_PCT` | `0.12` | Max 12% of portfolio per trade |
| `MIN_TRADE_USD` | `5.0` | Minimum trade size in USD |
| `SLIPPAGE_PCT` | `1` | Slippage tolerance for DEX swap |
| `GAS_RESERVE` | `0.003` | Native token reserved for gas |

### Risk Controls

| Parameter | Default | Description |
|---|---|---|
| `MIN_TRADE_INTERVAL` | `1800` | 30min cooldown between same-direction trades |
| `MAX_SAME_DIR_TRADES` | `3` | Max consecutive same-direction trades |
| `MAX_CONSECUTIVE_ERRORS` | `5` | Circuit breaker threshold |
| `COOLDOWN_AFTER_ERRORS` | `3600` | Cooldown after circuit breaker trips |
| `POSITION_MAX_PCT` | `65` | Block BUY when TOKEN_A > this % |
| `POSITION_MIN_PCT` | `35` | Block SELL when TOKEN_A < this % |

## Grid Calculation

```python
def calc_dynamic_grid(price, price_history):
    center = EMA(price_history, EMA_PERIOD)
    vol = stddev(price_history)
    vol_pct = vol / mean(price_history) * 100

    step = (VOLATILITY_MULTIPLIER * vol) / (GRID_LEVELS / 2)
    step = clamp(step, price * STEP_MIN_PCT, price * STEP_MAX_PCT)
    step = max(step, STEP_FLOOR)

    low  = center - step * (GRID_LEVELS / 2)
    high = center + step * (GRID_LEVELS / 2)
    return {center, step, levels, range: [low, high], vol_pct}

def price_to_level(price, grid):
    return clamp(int((price - grid.low) / grid.step), 0, GRID_LEVELS)
```

**Examples** (at price $2000):

| Volatility | stddev | Step | Grid Range | Behavior |
|---|---|---|---|---|
| Low (1.5%) | $30 | $25 | $1925–$2075 | Tight, catches small swings |
| Medium (3%) | $60 | $50 | $1850–$2150 | Normal operation |
| High (7%) | $140 | $120 | $1640–$2360 | Wide, avoids whipsaw |

## Trade Size

```python
def calc_trade_amount(direction, bal_a, bal_b, price):
    total_usd = bal_a * price + bal_b
    if direction == "BUY":
        max_amount = min(bal_b, total_usd * MAX_TRADE_PCT)
    else:  # SELL
        available = bal_a - GAS_RESERVE
        max_amount = min(available * price, total_usd * MAX_TRADE_PCT)
    return max_amount if max_amount >= MIN_TRADE_USD else None
```

## Level Update Rule (Critical)

| Outcome | Update level? | Rationale |
|---|---|---|
| Trade succeeded | Yes | Grid crossing consumed |
| Trade failed | No | Retry on next tick |
| Trade skipped (cooldown/limit) | No | Don't lose the crossing |

## PnL Tracking

```
total_pnl    = current_portfolio_value - initial_value - deposits
grid_profit += levels_crossed × step × trade_amount  (SELL only)
```

- `total_pnl`: true performance including unrealized gains
- `grid_profit`: estimated profit from grid mechanics only
- **Wrong**: `sell_total - buy_total` (this is net cash flow, not profit)

## State Schema

```json
{
  "grid": {"center": 2000, "step": 33.3, "levels": 6,
           "range": [1900, 2100], "vol_pct": 2.1},
  "grid_set_at": "ISO timestamp",
  "current_level": 3,
  "price_history": [1990, 2005, ...],
  "trades": [{"time": "...", "direction": "SELL", "price": 2050,
              "amount_usd": 25, "tx": "0x...",
              "grid_from": 2, "grid_to": 3}],
  "stats": {
    "total_trades": 15,
    "realized_pnl": 5.2,
    "grid_profit": 3.8,
    "initial_portfolio_usd": 1000
  },
  "last_trade_times": {"BUY": "...", "SELL": "..."},
  "consecutive_errors": 0
}
```

## Swap Execution Flow

```
1. Quote  → okx-dex-swap /aggregator/quote
2. Approve → okx-dex-swap /aggregator/approve-transaction (ERC-20 only)
3. Swap   → okx-dex-swap /aggregator/swap → get tx object
4. Sign   → Wallet (Privy server wallet / local key)
5. Broadcast → okx-onchain-gateway /pre-transaction/broadcast-transaction
```

Check `priceImpactPercent` from swap response before executing — reject if impact > slippage tolerance.

## Adapting to Different Pairs

| Consideration | What to adjust |
|---|---|
| Token decimals | USDC=6, DAI=18, WBTC=8 — affects amount conversion |
| Typical volatility | BTC lower vol → smaller `STEP_MIN/MAX_PCT`; meme coins → larger |
| Liquidity depth | Low liquidity → smaller `MAX_TRADE_PCT`, add price impact check |
| Gas costs | L1 vs L2: adjust `GAS_RESERVE` and `MIN_TRADE_USD` |
| Stablecoin pair | TOKEN/USDC pair: `STEP_MIN_PCT` can be much tighter (0.2%) |
| Rate limits | Add 300-500ms delay between consecutive OKX API calls |

## Anti-Patterns

| Pattern | Problem |
|---|---|
| Recalibrate every tick | Grid oscillates, no stable levels |
| Update level on failure/skip | Silently loses grid crossings |
| No position limits | Trending market → 100% one-sided |
| Fixed step in volatile market | Too small → over-trades; too large → never triggers |
| `sell - buy` as PnL | Net cash flow ≠ profit |
| No cooldown | Rapid swings cause burst of trades eating slippage |
