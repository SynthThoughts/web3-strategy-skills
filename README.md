# Web3-Skills

Reusable Web3 trading skills for AI coding agents. Each skill is a self-contained `SKILL.md` that teaches an AI agent how to build, deploy, and operate a specific trading strategy.

## Skills

| Skill | Version | Runtime | Description |
|-------|---------|---------|-------------|
| [okx-strategy-factory](./okx-strategy-factory/) | v1.0.0 | Local (Claude Code / Cursor / Gemini CLI / Codex) | Meta-skill: coordinates 5 AI agents to develop, backtest, deploy, publish, and iterate OKX OnchainOS trading strategies. |
| [grid-trading](./grid-trading/) | v4.0.0 | Server (OpenClaw / VPS cron) | Dynamic grid trading on EVM L2 chains. Multi-timeframe analysis, trend-adaptive sizing, smart money signals. Sharpe 4.45. |
| [polymarket-arb-scanner](./polymarket-arb-scanner/) | v1.0.0 | Server (OpenClaw / VPS cron) | Three-layer arbitrage detection on Polymarket CLOB: single-condition, neg-risk multi-outcome, and cross-market implication. |

## How They Fit Together

```
┌─────────────────────────────────────────────────────────┐
│  LOCAL: Your IDE / Terminal                              │
│                                                          │
│  okx-strategy-factory (meta-skill)                       │
│  ├── Strategy Agent   → writes trading logic             │
│  ├── Backtest Agent   → validates with historical data   │
│  ├── Publish Agent    → packages as standalone Skill     │
│  ├── Infra Agent      → deploys to server ──────────┐    │
│  └── Iteration Agent  → reviews & optimizes    ◄────┤    │
│                                                      │    │
└──────────────────────────────────────────────────────┤────┘
                                                       │
┌──────────────────────────────────────────────────────▼────┐
│  SERVER: VPS / OpenClaw                                   │
│                                                           │
│  grid-trading          (cron every 5min → tick → trade)   │
│  polymarket-arb-scanner (cron → scan → alert)             │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

## Installation

### 1. Strategy Factory (local development)

Install in your **local IDE** to let AI agents develop and manage strategies:

**ClawHub** (recommended for OpenClaw users):
```bash
npx clawhub install okx-strategy-factory
```

**Claude Code**:
```bash
# Project-level
cp -r okx-strategy-factory /path/to/project/.claude/skills/

# Global (available to all projects)
cp -r okx-strategy-factory ~/.claude/skills/
```

**Cursor**:
```bash
cp -r okx-strategy-factory /path/to/project/.cursor/skills/
```

**Gemini CLI**:
```bash
cp -r okx-strategy-factory /path/to/project/.gemini/skills/
```

After installing, tell your AI agent:
```
Use the okx-strategy-factory skill to develop a grid trading strategy for ETH/USDC on Base.
```

### 2. Trading Strategies (server deployment)

Install on your **server / VPS** to run strategies 24/7:

**ClawHub** (recommended):
```bash
npx clawhub install grid-trading
npx clawhub install polymarket-arb-scanner
```

**OpenClaw with cron** (grid-trading example):
```bash
# Install skill
cp -r grid-trading ~/.openclaw/skills/

# Deploy strategy script
cp grid-trading/references/eth_grid_v4.py ~/.openclaw/scripts/

# Register cron jobs
openclaw cron add --name eth-grid-tick \
  --schedule "*/5 * * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v4.py tick"

openclaw cron add --name eth-grid-daily \
  --schedule "0 0 * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v4.py report"
```

**System crontab** (without OpenClaw):
```bash
# Copy script to server
scp grid-trading/references/eth_grid_v4.py user@your-vps:~/scripts/

# Add to crontab
crontab -e
# */5 * * * * cd ~/scripts && python3 eth_grid_v4.py tick >> /tmp/grid.log 2>&1
# 0 0 * * *   cd ~/scripts && python3 eth_grid_v4.py report >> /tmp/grid.log 2>&1
```

**One-click installer** (auto-detects platform):
```bash
cd grid-trading
./install.sh                          # Auto-detect
./install.sh --platform openclaw      # OpenClaw + cron registration
./install.sh --platform claude        # Claude Code
```

### 3. Just the Knowledge (any AI agent)

Each `SKILL.md` is plain Markdown. Paste it into any AI agent's system prompt:

```bash
cat grid-trading/SKILL.md | pbcopy   # Copy to clipboard on macOS
```

## Prerequisites

| Requirement | For | Install |
|-------------|-----|---------|
| onchainos CLI | grid-trading, strategy-factory | `npx skills add okx/onchainos-skills` |
| OKX API Key | All trading skills | Via 1Password or env vars |
| OnchainOS Wallet | grid-trading | `onchainos wallet login` |
| Python 3.10+ | Strategy scripts | System package manager |
| VPS (optional) | 24/7 trading | Any Linux server |
| 1Password CLI (optional) | Secure credential management | `brew install 1password-cli` |

## Skill Format

```
skill-name/
├── SKILL.md          # Core knowledge (YAML frontmatter + architecture + algorithms)
├── references/       # Detailed docs: CLI reference, algorithms, risk controls
├── roles/            # Agent role definitions (strategy-factory only)
├── assets/           # Templates and resources
├── hooks/            # Task gate scripts (strategy-factory only)
├── install.sh        # Multi-platform installer
└── README.md         # User-facing install and usage guide
```

## Contributing

PRs welcome. To add a new skill:

1. Create a folder named after your strategy
2. Write a `SKILL.md` — focus on teaching the AI agent the "why" alongside the "how"
3. Add `references/` for detailed docs, `install.sh` for easy setup
4. Include anti-patterns and failure modes you've encountered

## License

Apache-2.0
