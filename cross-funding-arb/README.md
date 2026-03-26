# Cross-Exchange Funding Rate Arbitrage

跨交易所资金费率套利策略。在费率低的交易所做多永续、费率高的做空，Delta-neutral 赚取 funding spread。

## Features

- **自动扫描**：VarFunding API 实时发现 HL × Binance 套利机会
- **稳定性验证**：多次快照确认费率稳定后才开仓，防止瞬时波动
- **原子开仓**：先 HL 后 Binance，失败自动回滚，无单腿裸露风险
- **健康监控**：Delta 偏差 + Spread 监控 + 双腿一致性检查
- **自动切仓**：Spread 不利时平仓，下一 tick 自动寻找新机会
- **多渠道通知**：Discord embed + Telegram markdown，按 tier 分级推送

## Architecture

```
VarFunding API → Scanner → Stability Check → Deep Verify → Atomic Open
                                                              ↓
                                                    HL (EIP-712) + Binance (HMAC)
                                                              ↓
                                                    Health Monitor → Auto Close/Switch
```

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# 编辑 .env，填入 HL 私钥和 Binance API Key
```

编辑 `config.json` 调整预算和风控参数。

### 3. Test

```bash
# 查看当前状态
python3 -m funding.hl_cross_funding status

# 单次 tick（扫描 + 开仓/维护）
python3 -m funding.hl_cross_funding tick
```

### 4. Deploy

```bash
# ZeroClaw cron（推荐）
zeroclaw cron add --expr "*/5 * * * *" --shell \
  "cd ~/scripts/hyperliquid && set -a && . ./.env && set +a && python3 -m funding.hl_cross_funding tick"

# 或系统 crontab
*/5 * * * * cd ~/scripts/hyperliquid && set -a && . ./.env && set +a && python3 -m funding.hl_cross_funding tick >> /tmp/cross_funding.log 2>&1
```

## Commands

| Command | Description |
|---|---|
| `tick` | 主循环：扫描 → 验证 → 开仓/维护 |
| `report` | 日报：持仓、PnL、余额、费率 |
| `status` | 当前状态（优先读缓存） |

## Risk Controls

| Control | Description |
|---|---|
| 稳定性验证 | 3+ 快照 + std_ratio < 0.3 |
| 深度验证 | 实时费率 + 价格差 < 0.5% + 净 APR ≥ 10% |
| 保守预算 | min(两所) × 50% |
| 滑点 0.1% | 远低于常规 5%，保护套利利润 |
| HL Margin 重试 | size 减半最多 3 次 |
| 原子回滚 | BN 失败 → 自动平 HL |
| 熔断器 | 连续 5 错 → 冷却 1h |

## Prerequisites

- Python 3.10+
- Hyperliquid 账户 + 私钥
- Binance Futures 账户 + API Key（USDT-M 交易权限）
- 两所均需有足够保证金（建议 HL ≥ $300, Binance ≥ $450）

## License

Apache-2.0
