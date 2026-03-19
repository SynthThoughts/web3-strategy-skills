# Risk Controls Reference

Complete checklist of all risk controls in Grid Trading v4. These are evaluated in order during each `tick()` execution.

## Pre-Trade Checks (Abort Tick)

These checks run before any grid logic. If any triggers, the tick exits immediately.

### 1. Stop Condition (Hard Stop)

| Control | Parameter | Default | Trigger |
|---------|-----------|---------|---------|
| Stop-Loss | `STOP_LOSS_PCT` | 0.15 (15%) | Portfolio drops 15% below cost basis |
| Trailing Stop | `TRAILING_STOP_PCT` | 0.10 (10%) | Portfolio drops 10% from peak value |
| ~~Take-Profit~~ | ~~`TAKE_PROFIT_PCT`~~ | Removed (v4.1) | Not applicable to grid strategies |

**Behavior**: Sets `stop_triggered` in state. All subsequent ticks log + send red Discord alert + refuse trading until `resume-trading` command is issued.

**Cost basis calculation**:
```
cost_basis = initial_portfolio_usd + total_deposits_usd
pnl_pct = (current_total_usd - cost_basis) / cost_basis
```

**Peak tracking**: `portfolio_peak_usd` is updated on every tick to the max of current and previous peak.

### 2. Circuit Breaker

| Control | Parameter | Default | Trigger |
|---------|-----------|---------|---------|
| Error threshold | `MAX_CONSECUTIVE_ERRORS` | 5 | 5 consecutive errors |
| Cooldown period | `COOLDOWN_AFTER_ERRORS` | 3600s (1h) | After breaker trips |

**Behavior**: When `consecutive_errors >= 5`, trading pauses for 1 hour. Counter resets on any successful tick.

### 3. Data Validation Gate

| Check | Condition |
|-------|-----------|
| Price valid | Price is non-null and > 0 |
| Balance exists | At least one of ETH or USDC balance > 0 |
| Circuit breaker clear | `consecutive_errors < MAX_CONSECUTIVE_ERRORS` |
| Stop not triggered | `stop_triggered == null` |

---

## Trade-Level Safety Checks

These checks run after the grid determines a trade should execute.

### 4. Cooldown Timer

| Control | Parameter | Default | Description |
|---------|-----------|---------|-------------|
| Same-direction cooldown | `MIN_TRADE_INTERVAL` | 1800s (30min) | Minimum time between trades in the same direction |

```
if direction == last_direction:
    if now - last_trade_time[direction] < MIN_TRADE_INTERVAL:
        SKIP trade
```

**Reset**: Timer is per-direction. A BUY does not reset the SELL timer and vice versa.

### 5. Consecutive Same-Direction Limit

| Control | Parameter | Default | Description |
|---------|-----------|---------|-------------|
| Max consecutive | `MAX_SAME_DIR_TRADES` | 3 | Max consecutive trades in same direction |

**Reset conditions**:
- Grid was recalibrated since last trade
- More than 1 hour since last trade
- Direction changes

### 6. Anti-Repeat Guard

Prevents executing the same trade twice at the same grid boundary:

```
if direction == last_trade_direction AND level_boundary == last_trade_boundary:
    SKIP trade
```

### 7. Rapid Drop Protection

| Check | Condition | Action |
|-------|-----------|--------|
| Flash crash guard | Price dropped > 2% in last 30 minutes (6 ticks) | Skip BUY |

```python
if direction == "BUY":
    recent_prices = price_history[-6:]
    if max(recent_prices) > 0 and (max(recent_prices) - price) / max(recent_prices) > 0.02:
        SKIP "rapid_drop_protection"
```

### 8. Trend-Adaptive Position Limits (v4)

Position limits shift based on trend context:

| Trend | Max ETH % (block BUY above) | Min ETH % (block SELL below) |
|-------|----------------------------|------------------------------|
| Neutral | 70% | 30% |
| Bullish (strength 1.0) | 80% | 30% |
| Bearish (strength 1.0) | 70% | 25% |

```python
if eth_pct > position_max_pct:
    SKIP BUY ("position_limit")
if eth_pct < position_min_pct:
    SKIP SELL ("position_limit")
```

### 9. Minimum Trade Size

| Control | Parameter | Default |
|---------|-----------|---------|
| Floor | `MIN_TRADE_USD` | $5.00 |

Trades below $5 are skipped — not worth the gas and slippage cost.

---

## Sell-Specific Controls (v4)

### 10. Sell Momentum Protection

| Control | Parameter | Default | Trigger |
|---------|-----------|---------|---------|
| Momentum threshold | `SELL_MOMENTUM_THRESHOLD` | 0.005 (0.5%) | 1h momentum > 0.5% in bullish uptrend |

Skip sell if the market is actively rallying in a confirmed uptrend.

### 11. Sell Trailing Delay

| Control | Parameter | Default | Trigger |
|---------|-----------|---------|---------|
| Trail ticks | `SELL_TRAIL_TICKS` | 2 (10min) | Wait 2 ticks of stability before selling in uptrend |

Counter is per level-transition. Resets if price returns below the sell trigger level.

---

## Execution Safety

### 12. Transaction Simulation

Pre-simulate via `onchainos gateway simulate` before broadcast. Simulation is **diagnostic only** (non-blocking) — a simulation failure is logged but does not prevent execution.

### 13. Auto-Retry on Failure

| Control | Default | Description |
|---------|---------|-------------|
| Max retries | 1 | One automatic retry per tick |
| Retry delay | 3 seconds | Wait before retry |
| Fresh quote | Yes | Get new swap quote on retry |

Only retries if `failure_info.retriable == True`.

### 14. Level Update Rule

| Outcome | Update grid level? |
|---------|--------------------|
| Trade succeeded | Yes |
| Trade failed | **No** |
| Trade skipped (any reason) | **No** |
| Sell delayed (trailing/momentum) | **No** |

Critical: updating level on failure silently loses grid crossings.

---

## Monitoring & Alerting

### 15. Discord Notifications

| Event | Color | Frequency |
|-------|-------|-----------|
| Trade executed (BUY) | Blue | Every trade |
| Trade executed (SELL) | Green | Every trade |
| No trade (status) | Grey | Once per `QUIET_INTERVAL` (1h) |
| Stop triggered | Red | Immediately |
| Circuit breaker | Red | Immediately |

### 16. Structured JSON Output

Every tick emits a `---JSON---` block with status, market data, portfolio, and failure reasons. AI agents can parse this for automated monitoring and parameter tuning.

---

## Risk Control Flow Summary

```
tick() entry
  |
  [1] stop_triggered? -----> YES: log + red alert + RETURN
  |
  [2] check_stop_conditions -> triggered? set stop + alert + RETURN
  |
  [3] circuit_breaker? -----> YES: log + RETURN
  |
  [4] data_validation ------> FAIL: increment errors + RETURN
  |
  [5] MTF analysis (v4)
  |
  [6] grid decision
  |   |-- no level change -> no trade
  |   |-- level changed:
  |       |
  |       [7] cooldown check -----> SKIP
  |       [8] consecutive check --> SKIP
  |       [9] anti-repeat --------> SKIP
  |       [10] rapid drop --------> SKIP (BUY only)
  |       [11] position limits ---> SKIP
  |       [12] min trade size ----> SKIP
  |       [13] sell momentum -----> SKIP (SELL in uptrend)
  |       [14] sell trailing -----> SKIP (SELL in uptrend)
  |       |
  |       EXECUTE trade
  |       |-- success: update level + record
  |       |-- failure: retry once if retriable
  |
  [15] notification + JSON output
```
