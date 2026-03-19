# Adapter 接口规范

策略代码只调用这些抽象接口。实际通过 `onchainos` CLI 执行。

完整的 onchainos 命令参考 `Skills/grid-trading/SKILL.md` 的 Tool Wrapper 部分。

## Wallet

| 方法 | 输入 | 输出 |
|------|------|------|
| `getBalance(token)` | symbol 或 address | `{ balance, decimals }` |
| `approve(token, spender, amount)` | 地址 + 金额 | `{ txHash, gasUsed }` |
| `getAddress()` | — | 钱包地址 |
| `getNativeBalance()` | — | 原生代币余额 |
| `getTransactionHistory(opts)` | `{ limit?, startTime?, endTime? }` | 交易列表 |

onchainos 对应：`onchainos wallet balance`, `onchainos swap approve`, `onchainos wallet contract-call`

## DEX

| 方法 | 输入 | 输出 | 限制 |
|------|------|------|------|
| `swap(tokenIn, tokenOut, amount, slippage)` | 代币 + 金额 + 滑点% | `{ txHash, amountIn, amountOut, gasUsed, actualSlippage }` | 先 getQuote |
| `getPrice(pair)` | 如 "ETH/USDT" | `{ price, timestamp }` | ≤ 1次/秒/pair |
| `getQuote(tokenIn, tokenOut, amount)` | 代币 + 金额 | `{ estimatedOutput, priceImpact, route, gasEstimate }` | impact > tolerance 则中止 |

onchainos 对应：`onchainos swap swap`, `onchainos swap quote`, `onchainos market kline`

## Position Manager

| 方法 | 说明 |
|------|------|
| `open({ pair, side, size, stopLoss, takeProfit })` | 开仓，强制校验 risk-profile |
| `close(positionId)` | 平仓，返回 PnL |
| `getAll()` | 所有持仓 |
| `checkRiskTriggers()` | 扫描止损/止盈触发 |
