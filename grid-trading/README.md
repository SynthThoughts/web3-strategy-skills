# Grid Trading

Dynamic grid trading strategy for any token pair on EVM L2 chains via OKX DEX API.

## Features

- **Asymmetric grid steps** — buy-dense/sell-wide in bullish trends, reverse in bearish
- **Multi-timeframe trend analysis** — 5min price history + 1H K-line ATR
- **Trend-adaptive position sizing** — equal, martingale, anti-martingale, pyramid modes
- **Sell trailing optimization** — hold longer in strong uptrends
- **HODL Alpha tracking** — measure strategy vs simple buy-and-hold
- **Comprehensive risk controls** — stop-loss, take-profit, drawdown protection, circuit breakers
- **Discord notifications** — trade alerts, daily reports

## Architecture

```
Cron (5min) → Python script → onchainos CLI → OKX Web3 API → Chain
                  ↓                ↓
            state_v1.json    Wallet (TEE signing)
                  ↓
            MTF Analysis → Trend-Adaptive Grid → Discord
```

## Installation

**ClawHub** (recommended):
```bash
npx clawhub install grid-trading
```

**OpenClaw with cron**:
```bash
cp -r grid-trading ~/.openclaw/skills/
cp grid-trading/references/eth_grid_v1.py ~/.openclaw/scripts/

openclaw cron add --name eth-grid-tick \
  --schedule "*/5 * * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v1.py tick"

openclaw cron add --name eth-grid-daily \
  --schedule "0 0 * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v1.py report"
```

**System crontab**:
```bash
scp grid-trading/references/eth_grid_v1.py user@your-vps:~/scripts/

crontab -e
# */5 * * * * cd ~/scripts && python3 eth_grid_v1.py tick >> /tmp/grid.log 2>&1
# 0 0 * * *   cd ~/scripts && python3 eth_grid_v1.py report >> /tmp/grid.log 2>&1
```

## Directory Structure

```
grid-trading/
├── SKILL.md              # Core knowledge: algorithm, pipeline, config
└── references/
    ├── eth_grid_v1.py     # Production strategy script
    └── grid-algorithm.md  # Algorithm deep-dive: grid math, MTF, asymmetry
```

## Prerequisites

- onchainos CLI — `npx skills add okx/onchainos-skills`
- OKX API Key with DEX trading permissions
- OnchainOS Agentic Wallet with TEE signing
- Python 3.10+
- VPS (recommended for 24/7 operation)

## License

Apache-2.0
