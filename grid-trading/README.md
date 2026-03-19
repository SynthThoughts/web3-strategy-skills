# Grid Trading v4

Dynamic grid trading strategy for any token pair on EVM L2 chains via OKX DEX API. Designed as a reusable Skill for AI coding agents.

## What is this?

A `SKILL.md` that teaches AI agents (Claude Code, Cursor, Gemini CLI) how to build, deploy, debug, and tune a production grid trading bot. The skill covers the complete lifecycle: architecture, algorithms, parameter tuning, risk controls, and AI-assisted review.

## v4 Core Improvements

| # | Feature | Problem Solved |
|---|---------|----------------|
| 1 | **Multi-Timeframe Analysis** | No trend awareness — over-traded in trending markets |
| 2 | **Trend-Adaptive Strategy** | Fixed grid width in all markets led to poor alpha |
| 3 | **K-line/ATR Volatility** | Price history stddev less accurate than OHLC true range |
| 4 | **Smart Money Signals** | No external confirmation for trade decisions |
| 5 | **Sell Trailing Optimization** | Sold too early in uptrends (84.3% sell success vs 100% buy) |
| 6 | **HODL Alpha Tracking** | Could not measure grid vs pure hold performance |

## Backtest Results

10-day backtest (2026-03-06 to 2026-03-16, ETH +9.00%):

| Metric | Value |
|--------|-------|
| Sharpe Ratio | **4.45** |
| Annualized Return | +17.13% |
| Max Drawdown | 11.92% |
| Total Trades | 54 |
| Best Config | 6 levels + trend_adaptive |

## Installation

### One-Line Install

```bash
# From this directory:
./install.sh                          # Auto-detect platform
./install.sh --platform claude        # Claude Code
./install.sh --platform cursor        # Cursor
./install.sh --platform gemini        # Gemini CLI
./install.sh --platform claude --global  # Global install (~/.claude/skills/)
```

### Manual Install

Copy the skill files to your AI agent's skill directory:

**Claude Code**:
```bash
mkdir -p /path/to/project/.claude/skills/grid-trading
cp SKILL.md references/ assets/ /path/to/project/.claude/skills/grid-trading/
```

**Cursor**:
```bash
mkdir -p /path/to/project/.cursor/skills/grid-trading
cp SKILL.md references/ assets/ /path/to/project/.cursor/skills/grid-trading/
```

**Gemini CLI**:
```bash
mkdir -p /path/to/project/.gemini/skills/grid-trading
cp SKILL.md references/ assets/ /path/to/project/.gemini/skills/grid-trading/
```

### What Gets Installed

```
grid-trading/
├── SKILL.md              # Main skill document (all knowledge)
├── references/           # Detailed reference docs
│   ├── cli-reference.md  # onchainos CLI command reference
│   ├── grid-algorithm.md # Core algorithm deep-dive
│   └── risk-controls.md  # Risk control checklist
└── assets/
    └── report-template.md  # Daily report template
```

## Quick Start

After installation, ask your AI agent:

```
Use the grid-trading skill to create a grid bot for ETH/USDC on Base chain.
```

The AI agent will use the skill to:
1. Set up the project structure
2. Configure parameters for your pair
3. Implement the grid logic with all v4 features
4. Set up cron scheduling and Discord notifications

## Configuration

Key parameters (all tunable):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `GRID_LEVELS` | 6 | Number of grid levels |
| `GRID_TYPE` | arithmetic | Grid type (arithmetic / geometric) |
| `SIZING_STRATEGY` | trend_adaptive | Position sizing strategy |
| `MAX_TRADE_PCT` | 12% | Max portfolio per trade |
| `STEP_MIN_PCT` | 1.0% | Minimum grid step (% of price) |
| `STOP_LOSS_PCT` | 15% | Stop-loss threshold |
| `TRAILING_STOP_PCT` | 10% | Trailing stop from peak |
| `SELL_TRAIL_TICKS` | 2 | Sell delay ticks in uptrend |

See `SKILL.md` for the complete parameter reference.

## Prerequisites

- **onchainos CLI**: Installed and in PATH (`npm i -g @qingchencloud/openclaw-zh`)
- **OKX API Key**: With DEX trading permissions
- **Wallet**: OnchainOS Agentic Wallet with TEE signing
- **Python 3.10+**: For the trading script
- **Discord Bot** (optional): For notifications

## Architecture

```
Cron (5min) -> Python script -> onchainos CLI -> OKX Web3 API -> Chain
                  |                |
            state_v4.json    Wallet (TEE signing)
                  |
            MTF Analysis + Signal Integration
                  |
            Trend-Adaptive Grid Decision
                  |
            Discord + JSON output
```

## Skill Pattern

This skill uses a **Pipeline + Tool Wrapper** composite pattern:

- **Pipeline**: 5-step execution flow (Data -> MTF Analysis -> Grid Decision -> Execution -> Notification)
- **Tool Wrapper**: onchainos CLI reference for swap, quote, balance, signal, and K-line operations

## License

Apache-2.0
