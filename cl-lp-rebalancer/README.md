# CL LP Rebalancer

[中文](./README_CN.md)

Concentrated liquidity LP auto-rebalancer for Uniswap V3 positions on EVM L2 chains via OKX DEX API.

## Features

- **Volatility-adaptive range** — tighter range in low-vol (higher capital efficiency), wider in high-vol (less rebalancing, less IL)
- **Trend-asymmetric adjustment** — shift range in trend direction to reduce impermanent loss
- **Multi-timeframe analysis** — 5min price history + 1H K-line ATR
- **Full rebalance pipeline** — claim fees → remove liquidity → swap ratio → add liquidity
- **Gas-aware triggers** — only rebalance when expected fee gain exceeds gas cost
- **Discord notifications** — rebalance alerts, position status

## Architecture

```
Cron (5min) → Python script → onchainos CLI → OKX Web3 API → Chain
                  ↓                ↓
            state_v1.json    Wallet (TEE signing)
                  ↓
            Price + Vol Analysis → Range Calculation → Rebalance → Discord
```

## How It Differs from Grid Trading

| Dimension | Grid Trading | CL LP Rebalancer |
|-----------|-------------|------------------|
| Revenue source | Grid spread (buy low, sell high) | LP fees (market making) |
| On-chain ops | Swap buy/sell | Add/remove liquidity + claim fees |
| Core params | Grid spacing, levels | Range width, tick spacing |
| Position form | Token balances | NFT position (LP token) |
| Risk profile | Missing one-sided moves | Impermanent loss (IL) |

## Installation

**ClawHub** (recommended):
```bash
npx clawhub install cl-lp-rebalancer
```

**Manual**:
```bash
cp -r cl-lp-rebalancer ~/.openclaw/skills/
```

## Directory Structure

```
cl-lp-rebalancer/
├── SKILL.md      # Core knowledge: algorithm, pipeline, config
├── install.sh    # Multi-platform installer
└── references/   # Detailed docs
```

## Prerequisites

- onchainos CLI — `npx skills add okx/onchainos-skills`
- OKX API Key with DEX trading permissions
- OnchainOS Agentic Wallet with TEE signing
- Python 3.10+
- VPS (recommended for 24/7 operation)

## License

Apache-2.0
