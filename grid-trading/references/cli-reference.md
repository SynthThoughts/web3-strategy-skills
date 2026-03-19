# onchainos CLI Reference

Complete command reference for the `onchainos` CLI used by Grid Trading v4.

## Prerequisites

```bash
# Verify installation
which onchainos

# Required environment variables
export OKX_API_KEY="..."
export OKX_SECRET_KEY="..."
export OKX_PASSPHRASE="..."
```

All commands return JSON. The wrapper function `onchainos_cmd()` handles parsing, timeout, and error normalization.

---

## swap quote — Get Price / Quote

**Purpose**: Fetch the current swap price or a detailed quote with TX data.

```bash
# Simple price check (1 ETH -> USDC)
onchainos swap quote \
  --from 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee \
  --to 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913 \
  --amount 1000000000000000000 \
  --chain base
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--from` | address | Yes | Source token address (`0xeee...eee` for native ETH) |
| `--to` | address | Yes | Destination token address |
| `--amount` | string | Yes | Amount in smallest unit (wei for ETH, 1e6 for USDC) |
| `--chain` | string | Yes | Chain name (`base`, `arbitrum`, `optimism`, etc.) |

**Output**:
```json
{
  "ok": true,
  "data": [{
    "toTokenAmount": "2090450000",
    "estimateGasFee": "0.000021"
  }]
}
```

**Price extraction**: `toTokenAmount / 10^(to_token_decimals)` — for USDC: `2090450000 / 1e6 = $2090.45`

---

## swap swap — Execute Swap

**Purpose**: Get a full swap quote with TX data for signing and broadcasting.

```bash
onchainos swap swap \
  --from 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee \
  --to 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913 \
  --amount 50000000000000000 \
  --chain base \
  --wallet 0x50125b41c77d242bf7885950058a1dd1e0afd937 \
  --slippage 1
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--from` | address | Yes | Source token address |
| `--to` | address | Yes | Destination token address |
| `--amount` | string | Yes | Amount in smallest unit |
| `--chain` | string | Yes | Chain name |
| `--wallet` | address | Yes | Wallet address for the trade |
| `--slippage` | number | Yes | Slippage tolerance in percent (e.g., `1` = 1%) |

**Output**:
```json
{
  "ok": true,
  "data": [{
    "tx": {
      "to": "0x routerAddress",
      "data": "0x calldata...",
      "value": "50000000000000000",
      "gas": "180000"
    }
  }]
}
```

**Usage**: Extract `tx.to`, `tx.data`, `tx.value` to pass to `wallet contract-call` for signing.

---

## swap approve — Token Approval

**Purpose**: Approve an ERC-20 token for the DEX router (required before selling non-native tokens like USDC).

```bash
onchainos swap approve \
  --token 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913 \
  --amount 115792089237316195423570985008687907853269984665640564039457584007913129639935 \
  --chain base
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--token` | address | Yes | Token contract address to approve |
| `--amount` | string | Yes | Approval amount (use max uint256 for unlimited) |
| `--chain` | string | Yes | Chain name |

**Output**: TX data for approval transaction.

**Note**: The bot caches approved routers in `state.approved_routers[]` to avoid redundant approvals.

---

## market kline — K-line / Candlestick Data

**Purpose**: Fetch OHLCV candlestick data for ATR-based volatility calculation.

```bash
onchainos market kline \
  --address 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee \
  --chain base \
  --bar 1H \
  --limit 24
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--address` | address | Yes | Token address |
| `--chain` | string | Yes | Chain name |
| `--bar` | string | Yes | Candle interval: `1m`, `5m`, `15m`, `1H`, `4H`, `1D` |
| `--limit` | number | Yes | Number of candles to fetch (max varies by interval) |

**Output**:
```json
{
  "ok": true,
  "data": [
    {"ts": 1710000000, "open": "2080.5", "high": "2095.2", "low": "2075.1", "close": "2090.4", "volume": "1234.56"},
    ...
  ]
}
```

**ATR calculation**: For each candle, True Range = max(high-low, |high-prev_close|, |low-prev_close|). ATR = SMA of True Range over N candles.

**Cache**: Results cached for 1 hour (`kline_cache` in state).

---

## signal list — Smart Money Signals

**Purpose**: Fetch on-chain smart money (whale, smart wallet) activity signals for trade confirmation.

```bash
onchainos signal list \
  --chain base \
  --wallet-type 1,2,3 \
  --token-address 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--chain` | string | Yes | Chain name |
| `--wallet-type` | string | Yes | Comma-separated wallet types: `1`=whale, `2`=smart money, `3`=fresh wallet |
| `--token-address` | address | Yes | Token to check signals for |

**Output**:
```json
{
  "ok": true,
  "data": [
    {
      "walletType": 1,
      "triggerWalletCount": 5,
      "soldRatioPercent": 20.5,
      "boughtRatioPercent": 79.5
    }
  ]
}
```

**Bullish score calculation**:
```
For each signal where triggerWalletCount >= SIGNAL_MIN_TRIGGER_WALLETS:
  buy_score = boughtRatioPercent / 100
  sell_score = soldRatioPercent / 100
  net = buy_score - sell_score (clamped 0-1)
bullish_score = weighted average across wallet types
```

**Cache**: Results cached for 15 minutes (`signal_cache` in state).

---

## gateway simulate — Transaction Simulation

**Purpose**: Dry-run a transaction to check for revert, gas estimation, and potential issues before broadcasting.

```bash
onchainos gateway simulate \
  --from 0x50125b41c77d242bf7885950058a1dd1e0afd937 \
  --to 0xRouterAddress \
  --data 0xcalldata... \
  --chain base
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--from` | address | Yes | Sender address |
| `--to` | address | Yes | Contract address |
| `--data` | hex | Yes | Transaction calldata |
| `--chain` | string | Yes | Chain name |
| `--value` | string | No | ETH value to send (in wei) |

**Output**:
```json
{
  "ok": true,
  "data": [{
    "failReason": null,
    "gasUsed": "150000"
  }]
}
```

**Note**: Simulation is diagnostic and non-blocking. A simulation failure logs a warning but does not prevent broadcast (real execution may still succeed due to MEV/timing differences).

---

## gateway broadcast — Transaction Broadcast

**Purpose**: Broadcast a signed transaction to the network.

```bash
onchainos gateway broadcast \
  --signed-tx 0xSignedTxHex... \
  --address 0x50125b41c77d242bf7885950058a1dd1e0afd937 \
  --chain base
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--signed-tx` | hex | Yes | Signed transaction hex |
| `--address` | address | Yes | Sender address |
| `--chain` | string | Yes | Chain name |

**Output**:
```json
{
  "ok": true,
  "data": [{
    "txHash": "0x..."
  }]
}
```

---

## wallet balance — Balance Query

**Purpose**: Fetch on-chain token balances for the configured wallet.

```bash
onchainos wallet balance --chain 8453
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--chain` | string | Yes | Chain ID (numeric: `8453` for Base) or chain name |

**Output**:
```json
{
  "ok": true,
  "data": [{
    "details": [{
      "tokenAssets": [
        {"symbol": "ETH", "balance": "0.134567"},
        {"symbol": "USDC", "balance": "257.33"}
      ]
    }]
  }]
}
```

**Note**: Uses chain ID (numeric) rather than chain name for this endpoint.

---

## wallet contract-call — Sign and Broadcast

**Purpose**: Sign a transaction with the TEE-backed wallet and broadcast in one step.

```bash
onchainos wallet contract-call \
  --to 0xRouterAddress \
  --chain 8453 \
  --input-data 0xcalldata... \
  --value 50000000000000000
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `--to` | address | Yes | Contract address |
| `--chain` | string | Yes | Chain ID |
| `--input-data` | hex | Yes | Transaction calldata |
| `--value` | string | No | ETH value in wei (for BUY trades) |

**Output**:
```json
{
  "ok": true,
  "data": [{
    "txHash": "0x..."
  }]
}
```

---

## Error Handling

All CLI calls go through `onchainos_cmd()` which normalizes output:

| Scenario | Behavior |
|----------|----------|
| Command returns JSON with `ok: true` | Return parsed data |
| Command returns JSON array | Wrap in `{"ok": true, "data": [...]}` |
| Command returns non-JSON | Return `None` |
| Command exits non-zero | Log stderr, return `None` |
| Command times out (30s default) | Log timeout, return `None` |

The calling function then converts `None` into a structured `failure_info`:

```python
failure_info = {
    "reason": str,      # "swap_quote_failed", "approval_failed", etc.
    "detail": str,      # human-readable context
    "retriable": bool,  # safe to auto-retry?
    "hint": str         # "transient_api_error", "retry_with_fresh_quote", "low_balance"
}
```
