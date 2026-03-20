---
name: cl-lp-rebalancer
description: "Uniswap V3 集中流动性 LP 自动调仓策略。基于波动率自适应范围宽度：低波动率收紧范围（高资本效率），高波动率放宽范围（减少调仓和 IL）。支持趋势不对称调整、多时间框架分析、自动 claim/remove/swap/deposit 全流程。适用于 EVM L2 链上 CL LP 管理、调仓、范围优化、手续费最大化场景。"
license: Apache-2.0
metadata:
  author: SynthThoughts
  version: "1.0.0"
  pattern: "pipeline, tool-wrapper"
  steps: "5"
---

# CL LP Auto-Rebalancer v1

Cron 驱动的 Uniswap V3 集中流动性自动调仓机器人，运行在 EVM L2 链上，通过 `onchainos` CLI 执行 DeFi 操作。核心思路：**波动率决定范围宽度** — 低波动率时收紧范围提高资本效率，高波动率时放宽范围减少调仓频率和无常损失。

每个 tick：获取价格 → 波动率分析 → 范围计算 → 调仓决策 → 执行调仓 → 报告。

## 与 Grid Trading 的区别

| 维度 | Grid Trading | CL LP Rebalancer |
|------|-------------|------------------|
| 收益来源 | 网格价差（低买高卖） | LP 手续费（做市） |
| 链上操作 | swap 买卖 | add/remove liquidity + claim fees |
| 核心参数 | 网格间距、层数 | 范围宽度、tick 间距 |
| 波动率响应 | 调整网格宽度 | 调整范围宽度 + 是否调仓 |
| 持仓形式 | 两种代币余额 | NFT position (LP token) |
| 风险特征 | 单边行情踏空 | 无常损失 (IL) |
| 调仓频率 | 每 tick 可能交易 | 仅在触发条件时调仓 |
| gas 敏感度 | 低（单次 swap） | 高（多步操作：claim+remove+swap+deposit） |

## Architecture

```
Cron (5min) → Python script → onchainos CLI → OKX Web3 API → Chain
                  ↓                ↓
            state_v1.json    Wallet (TEE signing)
                  ↓
            ┌──────────────┐
            │ Price Fetch   │ ← onchainos swap quote / market price
            │ K-line ATR    │ ← onchainos market kline (1H × 24)
            │ MTF Analysis  │ ← price_history (288 bars = 24h)
            └──────┬───────┘
                   ↓
            Range Calculation (vol-adaptive)
                   ↓
            Rebalance Decision
                   ↓
            ┌──────────────┐
            │ Claim Fees    │ ← onchainos defi claim
            │ Remove Liq    │ ← onchainos defi redeem
            │ Swap Ratio    │ ← onchainos swap swap
            │ Add Liq       │ ← onchainos defi deposit
            └──────┬───────┘
                   ↓
            Discord embed (notification)
```

**OKX Skill Dependencies** (via `onchainos` CLI — 处理认证、链解析、错误重试):

- Price: `onchainos market price --address <token> --chain <chain>`
- K-line: `onchainos market kline --address <token> --chain <chain> --bar 1H --limit 24`
- Quote: `onchainos swap quote --from <A> --to <B> --amount <amt> --chain <chain>`
- Swap: `onchainos swap swap --from <A> --to <B> --amount <amt> --chain <chain> --wallet <addr> --slippage <pct>`
- Approve: `onchainos swap approve --token <addr> --amount <amt> --chain <chain>`
- Pool Search: `onchainos defi search --chain <chain> --keyword <pair>`
- Pool Detail: `onchainos defi detail --investment-id <id> --chain <chain>`
- Calculate Entry: `onchainos defi calculate-entry --investment-id <id> --chain <chain> --tick-lower <tick> --tick-upper <tick>`
- Deposit: `onchainos defi deposit --investment-id <id> --chain <chain> --amount0 <amt> --amount1 <amt> --tick-lower <tick> --tick-upper <tick>`
- Redeem: `onchainos defi redeem --investment-id <id> --chain <chain> --token-id <id> --percent 100`
- Claim Fees: `onchainos defi claim --investment-id <id> --chain <chain> --token-id <id>`
- Positions: `onchainos defi positions --chain <chain>`
- Position Detail: `onchainos defi position-detail --investment-id <id> --chain <chain> --token-id <id>`

## Pipeline: Execution Steps

**CRITICAL RULE**: Steps MUST execute in order. Do NOT skip steps or proceed past a gate that has not been satisfied.

### Step 1: Data Acquisition

**Actions**:
1. Fetch current price via `onchainos market price` or `onchainos swap quote`
2. Fetch current position detail via `onchainos defi position-detail` (if position exists)
3. Update `price_history` (append, cap at 288 = 24h @ 5min)
4. Fetch on-chain balances via `onchainos wallet balance`

**Gate** (ALL must pass):
- [ ] Price is non-null and > 0
- [ ] Circuit breaker not active (`consecutive_errors < 5`)
- [ ] Stop not triggered (`stop_triggered == null`)

### Step 2: Volatility & Trend Analysis

**Actions**:
1. Fetch K-line data (1H candles, 24 bars) → compute ATR-based volatility (hourly cache)
2. Classify volatility: low (<1.5%), medium (1.5-3%), high (3-5%), extreme (>5%)
3. Compute multi-timeframe trend analysis (复用 grid-trading MTF):
   - Short EMA (25min), Medium EMA (1h), Long EMA (4h)
   - EMA alignment → trend direction (bullish/bearish/neutral) + strength (0-1)
   - 8h structure detection (uptrend/downtrend/ranging)
4. Compute 1h and 4h momentum

**Output**: `atr_pct` float, `vol_class` string, `mtf` dict

**Gate**:
- [ ] `atr_pct` is non-null and > 0
- [ ] `vol_class` is one of: low, medium, high, extreme
- [ ] `mtf` dict has `trend` and `strength` fields (graceful fallback to neutral)

### Step 3: Range Calculation

**Actions**:
1. Compute range width based on volatility class:
   - Low (<1.5%): `2 × ATR` each side → tight range, max capital efficiency
   - Medium (1.5-3%): `3 × ATR` each side → balanced
   - High (3-5%): `5 × ATR` each side → wide range, fewer rebalances
   - Extreme (>5%): `8 × ATR` each side → safety first
2. Apply trend asymmetry (if trend strength > 0.3):
   - Bullish: upper side wider, lower side tighter (跟随上涨空间)
   - Bearish: lower side wider, upper side tighter (防御下跌空间)
3. Convert price range to tick range (aligned to pool's `tick_spacing`)
4. Compute capital efficiency estimate: `price / (upper - lower)`

**Output**: `tick_lower`, `tick_upper`, `range_width_pct`, `capital_efficiency`

**Gate**:
- [ ] `tick_lower < current_tick < tick_upper`
- [ ] Range width >= minimum (2 × tick_spacing)
- [ ] tick values aligned to pool's `tick_spacing`

### Step 4: Rebalance Decision

**Actions**:
1. If no existing position → always deploy (first run)
2. Check rebalance triggers (in priority order):
   - **Out of range**: price < lower or price > upper → MUST rebalance
   - **Volatility shift**: ATR changed >30% from position creation → adaptive rebalance
   - **Time decay**: position age > 24h → maintenance rebalance
3. Anti-churn checks:
   - Position age >= `MIN_POSITION_AGE` (2h)
   - Rebalance count < `MAX_REBALANCES_24H` (6/day)
   - Gas cost < `GAS_TO_FEE_RATIO` × expected fees (50%)
   - New range differs >5% from current range
4. Check stop conditions: stop-loss, trailing stop, IL tolerance

**Gate**:
- [ ] Rebalance trigger identified, OR no rebalance needed (skip to Step 5)
- [ ] All anti-churn checks passed (if rebalancing)
- [ ] No stop condition triggered

### Step 5: Execution & Notification

**Actions** (if rebalancing):
1. Claim accumulated fees: `onchainos defi claim`
2. Remove liquidity: `onchainos defi redeem --percent 100`
3. Calculate target token ratio for new range: `onchainos defi calculate-entry`
4. Swap to correct ratio: `onchainos swap swap` (if needed)
5. Deposit at new range: `onchainos defi deposit`
6. On failure at any sub-step: emergency fallback deploy at 3× normal width
7. Record rebalance in state, update position info

**Actions** (always):
8. Calculate performance metrics (fees claimed, IL, net yield, time-in-range)
9. Build structured JSON output for AI agent parsing
10. Send Discord embed (green=rebalance success, grey=no action, red=stop/error)

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

### Core DeFi Operations

| Operation | Command | Key Parameters |
|---|---|---|
| Search Pools | `onchainos defi search --chain base --keyword "ETH USDC"` | chain, keyword |
| Pool Detail | `onchainos defi detail --investment-id <id> --chain base` | investment-id |
| Calculate Entry | `onchainos defi calculate-entry --investment-id <id> --chain base --tick-lower <t> --tick-upper <t>` | ticks, amounts |
| Deposit | `onchainos defi deposit --investment-id <id> --chain base --amount0 <a> --amount1 <a> --tick-lower <t> --tick-upper <t>` | amounts, ticks |
| Redeem | `onchainos defi redeem --investment-id <id> --chain base --token-id <nft> --percent 100` | token-id, percent |
| Claim Fees | `onchainos defi claim --investment-id <id> --chain base --token-id <nft>` | token-id |
| My Positions | `onchainos defi positions --chain base` | chain |
| Position Detail | `onchainos defi position-detail --investment-id <id> --chain base --token-id <nft>` | token-id |

### Market & Swap Operations

| Operation | Command | Key Parameters |
|---|---|---|
| Get Price | `onchainos market price --address <token> --chain base` | token address |
| Get K-line | `onchainos market kline --address <token> --chain base --bar 1H --limit 24` | bar size, limit |
| Swap Quote | `onchainos swap quote --from <A> --to <B> --amount <amt> --chain base` | tokens, amount |
| Execute Swap | `onchainos swap swap --from <A> --to <B> --amount <amt> --chain base --wallet <addr> --slippage 1` | wallet, slippage |
| Approve Token | `onchainos swap approve --token <addr> --amount <amt> --chain base` | token, amount |

### Error Handling Protocol

Every function returns `(result, failure_info)`. Failure info is structured:

```python
failure_info = {
    "reason": str,      # machine-readable: "claim_failed", "redeem_failed", "deposit_failed", etc.
    "detail": str,      # human-readable context
    "retriable": bool,  # safe to auto-retry?
    "hint": str         # "transient_api_error", "retry_with_fresh_quote", "insufficient_balance"
}
```

Auto-retry policy: 1 retry for `retriable=True` with 3s delay.

Rebalance failure fallback: if deposit fails after remove, emergency deploy at 3× normal width.

## Tunable Parameters

### Range Configuration

| Parameter | Default | Description |
|---|---|---|
| `VOL_MULTIPLIER_LOW` | `2.0` | ATR multiplier for low volatility (<1.5%) |
| `VOL_MULTIPLIER_MED` | `3.0` | ATR multiplier for medium volatility (1.5-3%) |
| `VOL_MULTIPLIER_HIGH` | `5.0` | ATR multiplier for high volatility (3-5%) |
| `VOL_MULTIPLIER_EXTREME` | `8.0` | ATR multiplier for extreme volatility (>5%) |
| `VOL_THRESHOLD_LOW` | `1.5` | Low/medium volatility boundary (%) |
| `VOL_THRESHOLD_HIGH` | `3.0` | Medium/high volatility boundary (%) |
| `VOL_THRESHOLD_EXTREME` | `5.0` | High/extreme volatility boundary (%) |
| `TREND_ASYM_FACTOR` | `0.3` | Max trend asymmetry ratio (0=symmetric, 1=fully asymmetric) |
| `TREND_ASYM_THRESHOLD` | `0.3` | Minimum trend strength to activate asymmetry |

### Rebalance Triggers

| Parameter | Default | Description |
|---|---|---|
| `VOL_SHIFT_THRESHOLD` | `0.30` | Trigger if ATR changed >30% from position creation |
| `MAX_POSITION_AGE_H` | `24` | Force rebalance after 24 hours |
| `MIN_RANGE_CHANGE_PCT` | `0.05` | Skip rebalance if new range <5% different |

### Anti-Churn Controls

| Parameter | Default | Description |
|---|---|---|
| `MIN_POSITION_AGE` | `7200` | 2h minimum position hold time (seconds) |
| `MAX_REBALANCES_24H` | `6` | Maximum rebalances per 24h period |
| `GAS_TO_FEE_RATIO` | `0.5` | Skip if gas > 50% of expected fees |

### Multi-Timeframe Analysis

| Parameter | Default | Description |
|---|---|---|
| `MTF_SHORT_PERIOD` | `5` | 5-bar EMA (25min @ 5min tick) |
| `MTF_MEDIUM_PERIOD` | `12` | 12-bar EMA (1h @ 5min tick) |
| `MTF_LONG_PERIOD` | `48` | 48-bar EMA (4h @ 5min tick) |
| `MTF_STRUCTURE_PERIOD` | `96` | 96-bar (8h) for structure detection |

### Risk Controls

| Parameter | Default | Description |
|---|---|---|
| `STOP_LOSS_PCT` | `0.15` | Stop if portfolio drops 15% below cost basis |
| `TRAILING_STOP_PCT` | `0.10` | Stop if portfolio drops 10% from peak |
| `MAX_IL_TOLERANCE_PCT` | `0.05` | Hard stop if IL exceeds 5% |
| `MAX_CONSECUTIVE_ERRORS` | `5` | Circuit breaker threshold |
| `COOLDOWN_AFTER_ERRORS` | `3600` | 1h cooldown after circuit breaker trips |
| `GAS_RESERVE` | `0.003` | Native token reserved for gas |

### Execution

| Parameter | Default | Description |
|---|---|---|
| `SLIPPAGE_PCT` | `1` | Slippage tolerance for swaps |
| `EMERGENCY_WIDTH_MULT` | `3.0` | Emergency fallback range = 3× normal width |
| `DRY_RUN` | `false` | Fetch real data but simulate operations |

## Risk Control Flow

```
[1] stop_triggered → refuse all operations, alert Discord
[2] circuit_breaker (consecutive_errors >= 5) → 1h cooldown, refuse
[3] data validation (price/balance/position null) → refuse
[4] stop-loss / trailing-stop / IL tolerance → set stop_triggered, alert
[5] rebalance frequency (>6/day) → skip rebalance
[6] position age (<2h) → skip rebalance
[7] gas cost check (>50% of expected fees) → skip rebalance
[8] minimum range change (<5%) → skip rebalance
[9] execute rebalance → success / failure with emergency fallback
```

## Operational Interface

### Sub-Commands

| Command | Purpose | Trigger |
|---|---|---|
| `tick` | Main loop: price → analysis → range → decide → execute → report | Cron every 5min |
| `status` | Print current position, range, metrics, trend | On demand |
| `report` | Generate daily performance report (Chinese) | Cron daily 08:00 CST |
| `history` | Show rebalance history | On demand |
| `reset` | Close position and redeploy from scratch | Manual |
| `close` | Fully exit position, claim fees, withdraw all | Manual |
| `analyze` | Output detailed JSON analysis (vol, range, efficiency) | AI agent |

```python
COMMANDS = {
    "tick": tick, "status": status, "report": report,
    "history": history_cmd, "reset": reset, "close": close,
    "analyze": analyze
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
  "status": "rebalanced" | "no_action" | "out_of_range" | "error" | "stopped",
  "market": {
    "price": 2090.45,
    "atr_pct": 1.8,
    "vol_class": "medium",
    "trend": "bullish",
    "trend_strength": 0.65,
    "momentum_1h": 0.35,
    "structure": "uptrend"
  },
  "position": {
    "token_id": "123456",
    "tick_lower": -198120,
    "tick_upper": -197400,
    "price_lower": 1950.0,
    "price_upper": 2150.0,
    "age_hours": 4.5,
    "in_range": true,
    "distance_to_edge": 0.35
  },
  "range": {
    "current_width_pct": 9.8,
    "optimal_width_pct": 10.2,
    "capital_efficiency": 10.2
  },
  "trigger": "none" | "out_of_range" | "vol_shift" | "time_decay",
  "rebalance": {
    "executed": false,
    "fees_claimed_usd": 1.25,
    "gas_cost_usd": 0.03,
    "il_realized_pct": 0.12
  },
  "stats": {
    "total_rebalances": 5,
    "total_fees_claimed_usd": 15.30,
    "total_gas_spent_usd": 0.45,
    "time_in_range_pct": 92.5,
    "net_yield_usd": 14.85,
    "net_yield_annualized_pct": 18.5
  }
}
```

### Discord Notification

Two formats:

**Rebalance executed** (colored embed):
- Green = successful rebalance
- Fields: price, old range → new range, fees claimed, gas cost, IL, net yield, time-in-range %, capital efficiency

**No action** (grey compact):
- One-line: price · range · edge distance · vol class · trend · fees accrued
- Sent once per `QUIET_INTERVAL` (default 1 hour)

**Error/Stop** (red embed):
- Error detail, stop reason, last known position

## State Schema

```json
{
  "version": 1,
  "pool": {
    "investment_id": "uniswap-v3-base-eth-usdc-3000",
    "chain": "base",
    "chain_id": 8453,
    "token0": "0x4200000000000000000000000000000000000006",
    "token1": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "fee_tier": 3000,
    "tick_spacing": 60
  },
  "position": {
    "token_id": null,
    "tick_lower": null,
    "tick_upper": null,
    "price_lower": null,
    "price_upper": null,
    "created_at": null,
    "created_atr_pct": null,
    "created_vol_class": null
  },
  "price_history": [],
  "vol_history": [],
  "rebalance_history": [
    {
      "time": "ISO timestamp",
      "trigger": "out_of_range",
      "old_range": [-198120, -197400],
      "new_range": [-198300, -197100],
      "fees_claimed_usd": 1.25,
      "gas_cost_usd": 0.03,
      "il_realized_pct": 0.12
    }
  ],
  "stats": {
    "total_rebalances": 0,
    "total_fees_claimed_usd": 0,
    "total_gas_spent_usd": 0,
    "time_in_range_pct": 100,
    "net_yield_usd": 0,
    "initial_portfolio_usd": null,
    "portfolio_peak_usd": null,
    "started_at": null,
    "last_check": null
  },
  "errors": {
    "consecutive": 0,
    "cooldown_until": null
  },
  "stop_triggered": null,
  "kline_cache": null,
  "mtf_cache": null,
  "last_quiet_report": null
}
```

Key fields:
- `pool`: target pool configuration (chain, tokens, fee tier, tick spacing)
- `position.token_id`: NFT position ID (null if no active position)
- `position.created_atr_pct`: ATR at position creation (for vol shift detection)
- `rebalance_history`: full audit trail of all rebalances with costs
- `stats.time_in_range_pct`: key performance metric — % of ticks where price was in range
- `stats.net_yield_usd`: fees claimed minus gas spent minus IL
- `stop_triggered`: string describing trigger condition, or null

## Core Algorithm

```
1. Fetch current price
2. Fetch position detail (if exists)
3. Update price_history (cap at 288 = 24h)
4. Fetch K-line data (1H × 24) → compute ATR volatility (hourly cache)
5. Classify volatility → vol_class (low/medium/high/extreme)
6. Multi-timeframe analysis → trend/strength/momentum/structure
7. Compute optimal range:
   a. Base width = VOL_MULTIPLIER[vol_class] × ATR each side
   b. Apply trend asymmetry (upper/lower sides)
   c. Convert to ticks, align to tick_spacing
8. Check rebalance triggers:
   a. Out of range → must rebalance
   b. ATR shift > 30% → adaptive
   c. Position age > 24h → maintenance
9. Anti-churn gates (position age, frequency, gas cost, range change)
10. If rebalancing:
    a. Claim fees → remove liquidity → swap to ratio → deposit at new range
    b. On failure: emergency fallback at 3× width
11. Check stop conditions (stop-loss, trailing stop, IL tolerance)
12. Calculate performance metrics
13. Report status (JSON + Discord)
```

## Deployment

### OpenClaw Cron (recommended)

```bash
# Register tick (every 5 minutes)
openclaw cron add \
  --name "cl-lp-tick" \
  --schedule "*/5 * * * *" \
  --command "cd ~/.openclaw/scripts && python3 cl_lp_v1.py tick"

# Register daily report (08:00 CST = 00:00 UTC)
openclaw cron add \
  --name "cl-lp-daily" \
  --schedule "0 0 * * *" \
  --command "cd ~/.openclaw/scripts && python3 cl_lp_v1.py report"
```

### Systemd

```ini
[Unit]
Description=CL LP Rebalancer Tick
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /path/to/cl_lp_v1.py tick
Environment=OKX_API_KEY=... OKX_SECRET_KEY=... OKX_PASSPHRASE=...
```

```ini
[Unit]
Description=CL LP Rebalancer Tick Timer
[Timer]
OnCalendar=*:0/5
[Install]
WantedBy=timers.target
```

### Manual

```bash
# Single tick
python3 cl_lp_v1.py tick

# Dry run (fetch real data, simulate operations)
DRY_RUN=true python3 cl_lp_v1.py tick

# Status check
python3 cl_lp_v1.py status

# Close position and exit
python3 cl_lp_v1.py close
```

## Failure & Rollback

```
IF rebalance sub-step fails:
  1. Log failure reason to cl_lp_v1.log
  2. Increment errors.consecutive
  3. If errors.consecutive >= 5: trigger circuit breaker (1h cooldown)
  4. If failure after remove liquidity: emergency deploy at 3× normal width
     (priority: get funds back into a position, even if suboptimal)
  5. Report failure via Discord + JSON output
  6. On next tick: retry from last successful sub-step if possible
```

## Anti-Patterns

| Pattern | Problem |
|---|---|
| Rebalance every tick | Gas costs eat all fee income |
| Too tight range in high vol | Constant out-of-range, excessive rebalancing |
| Too wide range in low vol | Capital inefficiency, minimal fee capture |
| No minimum position age | Rapid back-and-forth rebalancing (churn) |
| Skip emergency fallback | Funds sit idle after failed rebalance (zero yield) |
| Ignore gas costs | L1 gas can exceed daily fee income |
| Symmetric range in trends | Miss upside in bull, excess downside in bear |
| No IL tracking | Cannot detect when IL exceeds fee income |
| Rebalance on every vol change | Minor ATR fluctuations cause unnecessary churn |
| No time-in-range tracking | Cannot measure strategy effectiveness |
