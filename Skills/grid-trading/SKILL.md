---
name: grid-trading
description: "Dynamic grid trading strategy for any token pair on EVM L2 chains via OKX DEX API. Covers grid modes (arithmetic/geometric), position sizing strategies (equal/martingale/anti-martingale/pyramid), comprehensive risk controls (stop-loss, take-profit, drawdown protection, circuit breakers), trade execution via OKX DEX aggregator, PnL calculation, and Discord notification. Use when creating, modifying, debugging, or tuning a grid trading bot."
license: Apache-2.0
metadata:
  author: SynthThoughts
  version: "3.0.0"
---

# Dynamic Grid Trading Strategy

Cron-driven grid bot for EVM L2 chains via `onchainos` CLI. Every tick: fetch price → compute grid level → execute swap if level crossed → report to Discord.

## Architecture

```
Cron (5min) → Python script → onchainos CLI → OKX Web3 API → Chain
                    ↓               ↓
              state.json      Wallet (Privy/local key)
                    ↓
              Discord embed (notification)
```

**OKX Skill Dependencies** (via `onchainos` CLI — handles auth, chain resolution, error retry):
- Price: `okx-dex-market` → `onchainos market price <token> --chain <chain>`
- Quote: `okx-dex-swap` → `onchainos swap quote --from <A> --to <B> --amount <amt> --chain <chain>`
- Swap: `okx-dex-swap` → `onchainos swap swap --from <A> --to <B> --amount <amt> --chain <chain> --wallet <addr> --slippage <pct>`
- Approve: `okx-dex-swap` → `onchainos swap approve --token <addr> --amount <amt> --chain <chain>`
- Simulate: `okx-onchain-gateway` → `onchainos gateway simulate --from <addr> --to <addr> --data <hex> --chain <chain>`
- Broadcast: `okx-onchain-gateway` → `onchainos gateway broadcast --signed-tx <hex> --address <addr> --chain <chain>`
- Balance: `okx-wallet-portfolio` → `onchainos portfolio all-balances <wallet> --chain <chain>`

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
| `GRID_MODE` | `"arithmetic"` | `"arithmetic"` (fixed $ step) or `"geometric"` (fixed % step) |
| `EMA_PERIOD` | `20` | EMA lookback for grid center |
| `VOLATILITY_MULTIPLIER` | `2.5` | Grid width = multiplier × stddev |
| `GRID_RECALIBRATE_HOURS` | `12` | Max hours before forced recalibration |

### Grid Modes

**Arithmetic (等差网格)**: Each level is a fixed USD distance apart. Good for narrow ranges.

```
levels = [center - N*step, ..., center - step, center, center + step, ..., center + N*step]
```

**Geometric (等比网格)**: Each level is a fixed percentage apart. Better for wide ranges because step size scales with price. At $2000 with 1% ratio, step = $20; at $3000, step = $30.

```python
def calc_geometric_grid(center, ratio_pct, levels):
    ratio = 1 + ratio_pct / 100  # e.g. 1.01 for 1%
    grid = []
    for i in range(-levels//2, levels//2 + 1):
        grid.append(center * (ratio ** i))
    return grid

def price_to_level_geometric(price, grid_levels_list):
    # Find nearest level below price
    for i, lvl in enumerate(grid_levels_list):
        if price < lvl:
            return max(0, i - 1)
    return len(grid_levels_list) - 1
```

| Parameter | Default | Description |
|---|---|---|
| `GEOMETRIC_RATIO_PCT` | `1.0` | Step as % of price (geometric mode only) |

**Choosing a mode:**

| Market | Recommended | Why |
|---|---|---|
| Tight range ($1900-$2100) | Arithmetic | Even spacing, predictable profit per grid |
| Wide range ($1500-$3000) | Geometric | Steps scale with price, avoids crowding at low end |
| High volatility | Geometric | Naturally wider steps at higher prices |
| Stablecoin pairs | Arithmetic | Fixed small steps (0.1-0.5%) |

### Adaptive Step Sizing

Step scales linearly with real-time volatility (arithmetic mode):

```
step = (VOLATILITY_MULTIPLIER × stddev) / (GRID_LEVELS / 2)
step = clamp(step, price × STEP_MIN_PCT, price × STEP_MAX_PCT)
step = max(step, STEP_FLOOR)
```

For geometric mode, ratio adapts similarly:

```
ratio_pct = (VOLATILITY_MULTIPLIER × vol_pct) / (GRID_LEVELS / 2)
ratio_pct = clamp(ratio_pct, RATIO_MIN_PCT, RATIO_MAX_PCT)
```

| Parameter | Default | Description |
|---|---|---|
| `STEP_MIN_PCT` | `0.012` | Step floor as fraction of price (1.2%) |
| `STEP_MAX_PCT` | `0.060` | Step cap as fraction of price (6%) |
| `STEP_FLOOR` | `5.0` | Absolute minimum step in USD |
| `VOL_RECALIBRATE_RATIO` | `0.3` | Recalibrate if vol shifts >30% from grid snapshot |
| `RATIO_MIN_PCT` | `0.5` | Min geometric ratio (0.5%) |
| `RATIO_MAX_PCT` | `5.0` | Max geometric ratio (5%) |

**Recalibration triggers (asymmetric):**
1. **Downside breakout**: Price < grid lower - step → recalibrate **immediately** (buying dips is grid's edge)
2. **Upside breakout**: Price > grid upper + step → require **N consecutive ticks** confirmation before recalibrating (anti-chase)
3. Grid age exceeds `GRID_RECALIBRATE_HOURS`
4. Current volatility deviates >30% from grid's recorded volatility

| Parameter | Default | Description |
|---|---|---|
| `UPSIDE_CONFIRM_TICKS` | `6` | Ticks (30min @ 5min interval) price must hold above grid before upside recalibration |
| `MAX_CENTER_SHIFT_PCT` | `0.03` | Max 3% grid center shift per recalibration (prevents chasing spikes) |

**Anti-chase mechanism:**
- Upside breakout counter resets if price returns to grid range before confirmation
- Even after confirmation, center shifts are capped to `MAX_CENTER_SHIFT_PCT` per recalibration
- Multiple recalibrations can gradually track a true trend, but a single spike cannot drag the grid

### Position Sizing Strategies

Controls how much to trade at each grid level. Strategy applies a multiplier to the base trade amount (`MAX_TRADE_PCT`).

| Strategy | Parameter | Description |
|---|---|---|
| `"equal"` | — | Every grid level trades the same amount (default v1 behavior) |
| `"martingale"` | `MARTINGALE_FACTOR` | Each consecutive same-direction trade increases by factor. BUY: 1x, 2x, 3x... Aggressive accumulation on dips |
| `"anti_martingale"` | `ANTI_MARTINGALE_FACTOR` | Each consecutive same-direction trade decreases. Reduces exposure as trend continues against you |
| `"pyramid"` | — | Largest position at grid center, tapering toward edges. Concentrates capital where mean-reversion is strongest |

```python
SIZING_STRATEGY = "pyramid"       # "equal" | "martingale" | "anti_martingale" | "pyramid"
MARTINGALE_FACTOR = 1.5           # multiplier per consecutive same-direction trade
ANTI_MARTINGALE_FACTOR = 0.6      # decay per consecutive same-direction trade
MARTINGALE_MAX_MULTIPLIER = 3.0   # cap to prevent oversized trades

def calc_size_multiplier(strategy, grid_level, grid_levels, consecutive_same_dir):
    if strategy == "equal":
        return 1.0
    elif strategy == "martingale":
        mult = MARTINGALE_FACTOR ** consecutive_same_dir
        return min(mult, MARTINGALE_MAX_MULTIPLIER)
    elif strategy == "anti_martingale":
        return max(0.3, ANTI_MARTINGALE_FACTOR ** consecutive_same_dir)
    elif strategy == "pyramid":
        # Peak at center, taper to edges
        center = grid_levels / 2
        distance = abs(grid_level - center) / center  # 0 at center, 1 at edges
        return max(0.3, 1.0 - 0.7 * distance)
    return 1.0
```

**Comparison at $500 portfolio, 12% base:**

| Level | Equal | Martingale (1.5x) | Anti-Mart (0.6x) | Pyramid |
|---|---|---|---|---|
| L3 (center) | $60 | $60 | $60 | $60 |
| L2 (1 below) | $60 | $90 | $36 | $46 |
| L1 (2 below) | $60 | $135 | $22 | $32 |
| L0 (bottom) | $60 | $180→capped | $13 | $18 |

### Trade Execution

| Parameter | Default | Description |
|---|---|---|
| `MAX_TRADE_PCT` | `0.12` | Max 12% of portfolio per trade (before sizing multiplier) |
| `MIN_TRADE_USD` | `5.0` | Minimum trade size in USD |
| `SLIPPAGE_PCT` | `1` | Slippage tolerance for DEX swap |
| `GAS_RESERVE` | `0.003` | Native token reserved for gas |

### Risk Controls

#### Basic Controls (v1)

| Parameter | Default | Description |
|---|---|---|
| `MIN_TRADE_INTERVAL` | `1800` | 30min cooldown between same-direction trades |
| `MAX_SAME_DIR_TRADES` | `3` | Max consecutive same-direction trades |
| `MAX_CONSECUTIVE_ERRORS` | `5` | Circuit breaker threshold |
| `COOLDOWN_AFTER_ERRORS` | `3600` | Cooldown after circuit breaker trips |
| `POSITION_MAX_PCT` | `65` | Block BUY when TOKEN_A > this % |
| `POSITION_MIN_PCT` | `35` | Block SELL when TOKEN_A < this % |

#### Stop-Loss & Take-Profit (v2)

Protects against large losses and locks in gains.

| Parameter | Default | Description |
|---|---|---|
| `STOP_LOSS_ENABLED` | `True` | Enable stop-loss protection |
| `STOP_LOSS_PCT` | `10.0` | Stop trading if portfolio drops >10% from cost basis |
| `STOP_LOSS_ACTION` | `"pause"` | `"pause"` (stop trading) or `"sell_all"` (convert to stablecoin) |
| `TAKE_PROFIT_ENABLED` | `False` | Enable take-profit |
| `TAKE_PROFIT_PCT` | `20.0` | Lock profits if portfolio gains >20% |
| `TAKE_PROFIT_ACTION` | `"pause"` | `"pause"` or `"sell_all"` |

```python
def check_stop_loss(total_usd, cost_basis, config):
    if not config.get("STOP_LOSS_ENABLED"):
        return None
    loss_pct = ((cost_basis - total_usd) / cost_basis) * 100
    if loss_pct >= config["STOP_LOSS_PCT"]:
        return {"triggered": "stop_loss", "loss_pct": loss_pct,
                "action": config["STOP_LOSS_ACTION"]}
    return None

def check_take_profit(total_usd, cost_basis, config):
    if not config.get("TAKE_PROFIT_ENABLED"):
        return None
    gain_pct = ((total_usd - cost_basis) / cost_basis) * 100
    if gain_pct >= config["TAKE_PROFIT_PCT"]:
        return {"triggered": "take_profit", "gain_pct": gain_pct,
                "action": config["TAKE_PROFIT_ACTION"]}
    return None
```

#### Drawdown Protection (v2)

Monitors peak portfolio value and pauses if drawdown exceeds threshold.

| Parameter | Default | Description |
|---|---|---|
| `DRAWDOWN_ENABLED` | `True` | Enable drawdown protection |
| `MAX_DRAWDOWN_PCT` | `8.0` | Pause if portfolio drops >8% from peak |
| `DRAWDOWN_RECOVERY_PCT` | `3.0` | Resume when drawdown recovers to <3% |

```python
def check_drawdown(total_usd, peak_usd, config):
    if not config.get("DRAWDOWN_ENABLED"):
        return None
    drawdown_pct = ((peak_usd - total_usd) / peak_usd) * 100
    if drawdown_pct >= config["MAX_DRAWDOWN_PCT"]:
        return {"triggered": "drawdown", "drawdown_pct": drawdown_pct,
                "peak_usd": peak_usd}
    return None
```

**State additions for v2 risk controls:**

```json
{
  "risk": {
    "peak_portfolio_usd": 550.0,
    "peak_at": "ISO timestamp",
    "stop_loss_triggered": false,
    "take_profit_triggered": false,
    "drawdown_paused": false,
    "drawdown_paused_at": null
  }
}
```

#### Risk Control Flow in tick()

```
1. Check stop-loss/take-profit → if triggered, execute action and halt
2. Check drawdown → if exceeded, pause (skip trading but keep monitoring)
3. If drawdown_paused and drawdown recovers → resume
4. Normal grid logic (cooldown, position limits, etc.)
```

## Grid Calculation

### Arithmetic Mode

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

### Geometric Mode

```python
def calc_geometric_grid(price, price_history):
    center = EMA(price_history, EMA_PERIOD)
    vol = stddev(price_history)
    vol_pct = vol / mean(price_history) * 100

    # Derive ratio from volatility
    ratio_pct = (VOLATILITY_MULTIPLIER * vol_pct) / (GRID_LEVELS / 2)
    ratio_pct = clamp(ratio_pct, RATIO_MIN_PCT, RATIO_MAX_PCT)
    ratio = 1 + ratio_pct / 100

    # Build geometric level boundaries
    levels = [center * (ratio ** i) for i in range(-GRID_LEVELS//2, GRID_LEVELS//2 + 1)]
    levels.sort()
    return {center, ratio_pct, levels, range: [levels[0], levels[-1]], vol_pct}

def price_to_level_geometric(price, grid_levels_list):
    for i in range(len(grid_levels_list) - 1):
        if price < grid_levels_list[i + 1]:
            return i
    return len(grid_levels_list) - 1
```

**Examples** (at price $2000):

| Volatility | stddev | Step (arith) | Ratio (geo) | Grid Range | Behavior |
|---|---|---|---|---|---|
| Low (1.5%) | $30 | $25 | 0.8% | $1925–$2075 | Tight, catches small swings |
| Medium (3%) | $60 | $50 | 1.5% | $1850–$2150 | Normal operation |
| High (7%) | $140 | $120 | 3.5% | $1640–$2360 | Wide, avoids whipsaw |

## Trade Size

```python
def calc_trade_amount(direction, bal_a, bal_b, price, size_multiplier=1.0):
    total_usd = bal_a * price + bal_b
    base_amount = total_usd * MAX_TRADE_PCT * size_multiplier
    if direction == "BUY":
        max_amount = min(bal_b, base_amount)
    else:  # SELL
        available = bal_a - GAS_RESERVE
        max_amount = min(available * price, base_amount)
    return max_amount if max_amount >= MIN_TRADE_USD else None
```

The `size_multiplier` comes from the active sizing strategy (equal/martingale/anti-martingale/pyramid).

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
  "version": 5,
  "grid": {"center": 2000, "step": 33.3, "levels": 6,
           "range": [1900, 2100], "vol_pct": 2.1,
           "mode": "geometric", "ratio_pct": 1.2,
           "grid_levels": [1882, 1905, 1928, ...]},
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
    "initial_portfolio_usd": 1000,
    "initial_price": 2115.0
  },
  "risk": {
    "peak_portfolio_usd": 550.0,
    "peak_at": "ISO timestamp",
    "stop_loss_triggered": false,
    "take_profit_triggered": false,
    "drawdown_paused": false,
    "drawdown_paused_at": null
  },
  "last_trade_times": {"BUY": "...", "SELL": "..."},
  "consecutive_errors": 0
}
```

Key v2 additions: `risk` block for stop-loss/take-profit/drawdown state, `initial_price` for ETH-denominated PnL, `grid.mode` and `grid.grid_levels` for geometric support.

## Execution Pipeline (via onchainos CLI)

The execution layer uses `onchainos` CLI (v1.0.3+) which handles OKX API authentication (HMAC-SHA256), chain index resolution, and structured JSON output. The bot script only needs to call CLI commands and parse JSON — no manual API signing.

### Prerequisites

```bash
# onchainos CLI must be installed and configured
which onchainos  # /Users/mfer/.local/bin/onchainos

# Auth via environment variables (or .env file)
OKX_API_KEY=...
OKX_SECRET_KEY=...
OKX_PASSPHRASE=...
```

### Step 1: Get Price

```bash
onchainos market price $ETH_ADDR --chain base
# Returns: {"price": "2090.45", ...}
```

Alternative for quote-based pricing (more accurate for trade sizing):
```bash
onchainos swap quote \
  --from $ETH_ADDR --to $USDC_ADDR \
  --amount 1000000000000000000 \
  --chain base
# Returns: {"toTokenAmount": "2090450000", ...}
# Parse: int(toTokenAmount) / 1e6 = $2090.45
```

### Step 2: Get Balances

```bash
onchainos portfolio all-balances $WALLET_ADDR --chain base
# Returns: [{"tokenAddress": "0xeee...", "balance": "134248000000000000", ...}, ...]
```

Fallback (direct RPC — if portfolio CLI unavailable):
```python
# ETH balance: JSON-RPC eth_getBalance
# USDC balance: JSON-RPC eth_call with balanceOf(address) selector 0x70a08231
```

### Step 3: Execute Swap

Full swap flow with safety checks:

```python
import subprocess, json

def onchainos_cmd(args: list[str]) -> dict | None:
    """Run onchainos CLI command, return parsed JSON."""
    result = subprocess.run(
        ["onchainos"] + args,
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0 and result.stdout.strip():
        return json.loads(result.stdout)
    return None

def execute_swap(direction, amount, price, chain="base"):
    from_token = ETH_ADDR if direction == "SELL" else USDC_ADDR
    to_token = USDC_ADDR if direction == "SELL" else ETH_ADDR

    # 3a. Get swap quote + tx data
    swap = onchainos_cmd([
        "swap", "swap",
        "--from", from_token, "--to", to_token,
        "--amount", str(amount),
        "--chain", chain,
        "--wallet", WALLET_ADDR,
        "--slippage", str(SLIPPAGE_PCT)
    ])
    if not swap or not swap.get("data"):
        return None, {"reason": "swap_quote_failed", "retriable": True}

    tx = swap["data"][0]["tx"]

    # 3b. Safety: check priceImpactPercent
    impact = float(swap["data"][0].get("priceImpactPercent", 0))
    if impact > SLIPPAGE_PCT:
        return None, {"reason": "price_impact_too_high", "detail": f"{impact}%"}

    # 3c. For BUY (ERC-20 input): ensure approval
    if direction == "BUY":
        approve = onchainos_cmd([
            "swap", "approve",
            "--token", USDC_ADDR,
            "--amount", str(amount),
            "--chain", chain
        ])
        if approve and approve.get("data"):
            # Sign and broadcast approval tx first
            approve_tx = approve["data"][0]
            approve_hash = sign_and_send(approve_tx)
            if not approve_hash:
                return None, {"reason": "approval_failed"}
            time.sleep(5)  # Wait for approval to confirm

    # 3d. Pre-simulate (diagnostic, non-blocking)
    sim = onchainos_cmd([
        "gateway", "simulate",
        "--from", WALLET_ADDR,
        "--to", tx["to"],
        "--data", tx["data"],
        "--chain", chain
    ])
    # Log simulation result but don't block on it

    # 3e. Sign tx via wallet provider
    tx_hash = sign_and_send(tx)
    return tx_hash, None if tx_hash else {"reason": "signing_failed"}
```

### Step 4: Sign Transaction (Wallet Provider)

The signing layer is **independent of onchainos** — it depends on your wallet provider:

**Privy Server Wallet:**
```python
def sign_and_send(tx: dict) -> str | None:
    """Send tx via Privy wallet API. Returns tx_hash."""
    auth = base64.b64encode(f"{PRIVY_APP_ID}:{PRIVY_SECRET}".encode()).decode()
    transaction = {
        "to": tx["to"],
        "data": tx["data"],
        "value": hex(int(tx.get("value", "0")))
    }
    # Map gas fields (Privy uses snake_case, type as integer)
    if tx.get("gas") and tx.get("gasPrice"):
        transaction["gas_limit"] = hex(int(int(tx["gas"]) * 1.5))
        transaction["gas_price"] = hex(int(tx["gasPrice"]))
        transaction["type"] = 0  # Legacy tx

    payload = {
        "method": "eth_sendTransaction",
        "caip2": f"eip155:{CHAIN_ID}",
        "params": {"transaction": transaction}
    }
    resp = curl_post(
        f"https://api.privy.io/v1/wallets/{WALLET_ID}/rpc",
        headers={"Authorization": f"Basic {auth}", "privy-app-id": PRIVY_APP_ID},
        json=payload
    )
    return resp.get("data", {}).get("hash") if resp else None
```

**Local Private Key (alternative):**
```python
from web3 import Web3
def sign_and_send_local(tx: dict, private_key: str) -> str | None:
    w3 = Web3(Web3.HTTPProvider(BASE_RPC))
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    return tx_hash.hex()
```

### Step 5: Verify & Track

```bash
# Track order status via OKX
onchainos gateway orders --address $WALLET_ADDR --chain base --order-id $ORDER_ID
# Returns: {"txStatus": "2"}  (1=Pending, 2=Success, 3=Failed)
```

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

## Operational Interface

The bot script exposes multiple sub-commands via CLI for human and AI agent interaction.

### Sub-Commands

| Command | Purpose | Trigger |
|---|---|---|
| `tick` | Main loop: price → grid check → trade → report | Cron every 5min |
| `status` | Print current grid state, balances, PnL | On demand |
| `report` | Generate daily performance report (Chinese) | Cron daily 08:00 CST |
| `history` | Show recent trade history | On demand |
| `reset` | Reset grid (recalibrate from scratch), keep trade history | Manual |
| `retry` | Retry last failed trade with fresh quote | AI agent / manual |
| `analyze` | Output detailed market analysis JSON for AI | AI agent |
| `deposit` | Manually record deposit/withdrawal for PnL tracking | Manual |

```python
COMMANDS = {
    "tick": tick, "status": status, "report": report,
    "history": history, "reset": reset, "retry": retry,
    "analyze": analyze, "deposit": deposit
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
  "status": "trade_executed" | "no_trade" | "cooldown" | "trade_failed" | ...,
  "market": {"price": 2090.45, "ema": 2085.3, "volatility_pct": 1.2, "trend": "bullish"},
  "portfolio": {"eth": 0.134, "usdc": 257.33, "total_usd": 538.0, "eth_pct": 52.1},
  "grid_level": 3,
  "direction": "SELL",        // only if trade attempted
  "tx_hash": "0x...",         // only if trade succeeded
  "failure_reason": "...",    // only if trade failed
  "retriable": true,          // hint for AI to call retry
  "success_rate": {"total_attempts": 182, "successes": 182, "rate_pct": 100.0}
}
```

AI agents should parse the `---JSON---` delimiter and use the structured data for decisions (e.g., auto-retry on retriable failures, alert on low success rate).

### Discord Notification

Two card formats pushed via Discord Bot API:

**Trade executed** (colored embed):
- Green = SELL, Blue = BUY
- Fields: price, level, total value, position, PnL, grid profit, BaseScan link

**No trade** (grey compact card):
- One-line: price · level · position · PnL · trade count
- Only sent once per `QUIET_INTERVAL` (default 1 hour)

```python
def send_discord_embed(embeds, channel_id):
    """Send via Discord Bot API. Token from ~/.openclaw/openclaw.json"""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    # POST with Authorization: Bot <token>
```

### Deposit/Withdrawal Detection

Automatically detects external balance changes (deposits, withdrawals, airdrops) by comparing actual balances against expected balances:

```
unexplained_change = (current_balance - last_balance) - sum(recorded_trades_since_last)
if abs(unexplained_change) > $100:  # threshold to filter gas/slippage noise
    record as deposit or withdrawal
    adjust PnL cost basis
```

### Logging

- File: `grid_bot.log` in script directory
- Rotation: simple half-file rotation at 1MB
- Format: `[YYYY-MM-DD HH:MM:SS] message`

## Adapting to Different Pairs

| Consideration | What to adjust |
|---|---|
| Token decimals | USDC=6, DAI=18, WBTC=8 — affects amount conversion |
| Typical volatility | BTC lower vol → smaller `STEP_MIN/MAX_PCT`; meme coins → larger |
| Liquidity depth | Low liquidity → smaller `MAX_TRADE_PCT`, add price impact check |
| Gas costs | L1 vs L2: adjust `GAS_RESERVE` and `MIN_TRADE_USD` |
| Stablecoin pair | TOKEN/USDC pair: `STEP_MIN_PCT` can be much tighter (0.2%) |
| Rate limits | Add 300-500ms delay between consecutive OKX API calls |

## PnL Tracking (v2 dual-denominated)

v2 tracks both USD and ETH denominated performance:

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

This enables answering: "Am I better off than just holding ETH?" (grid_alpha_usd > 0 = yes).

## AI Review & Optimization (AI 复盘优化)

AI agent should periodically review trading performance and suggest/apply optimizations. This is a structured workflow, not ad-hoc — run it weekly or when cumulative PnL stalls.

### Step 1: Pull & Pair Trades

Extract recent trades (e.g., last 2 days) and pair each BUY with its corresponding SELL to form **round trips**.

```python
# Matching logic: a SELL from level A→B matches a BUY from level B→A
buy_stack = []
round_trips = []
for trade in trades:
    if trade["direction"] == "BUY":
        buy_stack.append(trade)
    else:  # SELL
        # Find matching buy: buy's grid_to == sell's grid_from
        for j in range(len(buy_stack)-1, -1, -1):
            if buy_stack[j]["grid_to"] == trade["grid_from"]:
                matched_buy = buy_stack.pop(j)
                round_trips.append((matched_buy, trade))
                break
```

Output per round trip:
- **Spread**: `(sell_price - buy_price) / buy_price × 100%`
- **Hold time**: minutes between buy and sell
- **Status**: profit (spread > 0.3%), micro-profit (0 < spread < 0.3%), loss (spread < 0)

Unmatched buys = open positions → calculate unrealized PnL at current price.

### Step 2: Flag Anomalies

Classify each round trip:

| Flag | Condition | Meaning |
|---|---|---|
| `LOSS` | spread < 0 | Bought high, sold low — strategy failure |
| `MICRO` | 0 < spread < 0.3% | Profit too small to cover DEX costs |
| `GOOD` | spread ≥ 0.3% | Healthy grid profit |

**Key metrics to compute:**
- Win rate: `GOOD / total`
- Loss impact: `sum(loss_usd) vs sum(profit_usd)` — a few big losses can erase many small wins
- Micro-trade ratio: if > 30%, step size is likely too small

### Step 3: Root Cause Analysis

For each anomaly type, trace back to the strategy decision:

**LOSS trades — typical root causes:**

| Pattern | Root Cause | Fix |
|---|---|---|
| Buy @high, sell @low after grid recalibration | Grid chased a spike then recalibrated down | Increase `UPSIDE_CONFIRM_TICKS`, reduce `MAX_CENTER_SHIFT_PCT` |
| Buy @high in trending market, sell @low on reversal | Grid center EMA too reactive | Increase `EMA_PERIOD` or `GRID_RECALIBRATE_HOURS` |
| Loss during flash crash | Stop-loss not enabled or threshold too loose | Enable `STOP_LOSS`, tighten `STOP_LOSS_PCT` |

**MICRO trades — typical root causes:**

| Pattern | Root Cause | Fix |
|---|---|---|
| Many trades with < 0.2% spread | Step too small for DEX cost | Increase `STEP_MIN_PCT` |
| Rapid back-and-forth at same levels | Low volatility, grid too dense | Increase `MIN_TRADE_INTERVAL` or step floor |
| Trades cluster in 5-10 min windows | Cooldown too short | Increase `MIN_TRADE_INTERVAL` |

### Step 4: Parameter Tuning

Based on the analysis, adjust parameters. Guidelines:

```
STEP_MIN_PCT ≥ DEX_total_cost × 3
  where DEX_total_cost ≈ slippage + price_impact ≈ 0.1-0.3% on L2
  → STEP_MIN_PCT ≥ 0.009 to 0.012 depending on liquidity

UPSIDE_CONFIRM_TICKS = typical_spike_duration / tick_interval
  e.g., spikes last ~20min, tick=5min → confirm_ticks = 4-6

MAX_CENTER_SHIFT_PCT = step_pct × 2-3
  prevents center from jumping more than 2-3 grid steps per recalibration
```

### Step 5: Backtest Against History

Simulate the new parameters against the same historical data:

```python
# For each LOSS trade, check if new logic would have prevented it:
# 1. Would upside confirmation have blocked the recalibration?
#    → Count how many consecutive ticks price stayed above grid
# 2. Would center cap have limited the damage?
#    → Calculate capped center vs actual center, compare buy prices
# 3. Would higher step floor have filtered the trade?
#    → Check if trade's spread < new_step_min
```

Report: "N out of M loss trades would have been prevented" + estimated savings.

### Step 6: Apply & Monitor

1. Backup current script
2. Apply parameter changes
3. Force grid recalibration (clear `grid_set_at` in state)
4. Monitor first 24h of trades under new parameters
5. Re-run Step 1-2 to verify improvement

### Review Checklist (AI Agent Prompt)

When asked to review grid performance, follow this script:

```
1. Read grid_state.json and grid_bot.log from the bot's working directory
2. Filter trades to review window (default: last 48h)
3. Pair trades into round trips
4. Compute: win_rate, avg_spread, loss_count, micro_count, total_pnl
5. If loss_count > 0: trace each loss to recalibration events in grid_bot.log
6. If micro_ratio > 30%: recommend STEP_MIN_PCT increase
7. Check price_history for spike-and-reversal patterns near losses
8. Propose specific parameter changes with backtest evidence
9. On user approval: backup → patch → recalibrate → verify
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
