# Grid Trading — Setup Guide

## Quick Start

### 1. Install

```bash
openclaw skill install grid-trading
```

### 2. Configure

```bash
cd ~/.openclaw/skills/grid-trading/references
cp ../.env.example .env
# Edit .env — fill in your OKX API keys and wallet address
```

### 3. Verify onchainos

```bash
onchainos wallet login          # if not already logged in
onchainos wallet balance --chain base   # test connectivity
```

### 4. Test run

```bash
cd ~/.openclaw/skills/grid-trading/references
python3 eth_grid_v1.py status
```

### 5. Register cron

**Option A: ZeroClaw / OpenClaw cron**

```bash
zeroclaw cron add '*/5 * * * *' \
  'cd ~/.openclaw/skills/grid-trading/references && set -a && . ./.env && set +a && python3 eth_grid_v1.py tick' \
  --tz Asia/Shanghai

zeroclaw cron add '0 0 * * *' \
  '执行ETH网格策略日报: cd ~/.openclaw/skills/grid-trading/references && set -a && . ./.env && set +a && python3 eth_grid_v1.py report。将完整输出结果总结后回复我。' \
  --agent --tz Asia/Shanghai
```

**Option B: System crontab**

```bash
crontab -e
# Add:
# */5 * * * * cd ~/.openclaw/skills/grid-trading/references && set -a && . ./.env && set +a && python3 eth_grid_v1.py tick >> /tmp/grid.log 2>&1
# 0 0 * * *   cd ~/.openclaw/skills/grid-trading/references && set -a && . ./.env && set +a && python3 eth_grid_v1.py report >> /tmp/grid.log 2>&1
```

## Available Commands

| Command | Purpose | Trigger |
|---------|---------|---------|
| `tick` | Price check → grid decision → trade | Cron every 5min |
| `status` | Current state, balances, PnL | On demand |
| `report` | Daily performance report (Discord) | Cron daily |
| `history` | Recent trade history | On demand |
| `reset` | Recalibrate grid from scratch | Manual |
| `retry` | Retry last failed trade | Manual |
| `analyze` | Detailed market + round-trip analysis | AI agent |
| `deposit` | Record external deposit/withdrawal | Manual |
| `resume-trading` | Clear stop-loss and resume | Manual |

## Tuning

Edit `config.json` in the same directory to adjust parameters (grid levels, risk limits, etc.). See `SKILL.md` for full parameter documentation.
