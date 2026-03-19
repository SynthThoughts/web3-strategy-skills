# Web3-Skills

Reusable Web3 trading skills for AI coding agents (Claude Code, Cursor, etc.).

Each skill is a self-contained `SKILL.md` that teaches an AI agent how to build, debug, and extend a specific trading strategy — covering architecture, algorithms, parameters, risk controls, and anti-patterns.

## Skills

| Skill | Version | Pattern | Description |
|-------|---------|---------|-------------|
| [grid-trading](./grid-trading/) | v4.0.0 | Pipeline + Tool Wrapper | Dynamic grid trading on EVM L2 chains via OKX DEX API. v4 adds multi-timeframe trend analysis, trend-adaptive sizing, smart money signals, sell trailing optimization, and HODL Alpha tracking. Sharpe 4.45. |
| [polymarket-arb-scanner](./polymarket-arb-scanner/) | v1.0.0 | Tool Wrapper | Three-layer arbitrage detection on Polymarket CLOB: single-condition, neg-risk multi-outcome, and cross-market logical implication. |

## How to Use

### Quick Install (any skill with install.sh)

```bash
cd grid-trading
./install.sh                          # Auto-detect platform
./install.sh --platform claude        # Claude Code
./install.sh --platform cursor        # Cursor
./install.sh --platform gemini        # Gemini CLI
./install.sh --global                 # Install to ~/.<platform>/skills/
```

### Claude Code

```bash
cp -r grid-trading /path/to/your/project/.claude/skills/
```

### Cursor

```bash
cp -r grid-trading /path/to/your/project/.cursor/skills/
```

### Gemini CLI

```bash
cp -r grid-trading /path/to/your/project/.gemini/skills/
```

### Other AI Agents

Each `SKILL.md` is plain Markdown — paste it into any AI agent's system prompt or context window. No proprietary format required.

## Skill Format

```
skill-name/
├── SKILL.md              # Complete knowledge document with YAML frontmatter
├── references/           # Detailed reference docs (tool-wrapper pattern)
├── assets/               # Templates and resources (generator pattern)
├── install.sh            # One-click installer for all platforms
└── README.md             # User-facing documentation
```

A `SKILL.md` contains:

- **YAML frontmatter** — name, description, license, metadata
- **Architecture** — system diagram and data flow
- **Core algorithm** — pseudocode or real code snippets
- **Parameters** — tunable configs with defaults and rationale
- **Risk controls** — safety checks, filters, anti-patterns
- **Extension points** — how to adapt or build upon the skill

## Contributing

PRs welcome. To add a new skill:

1. Create a folder named after your strategy (e.g., `mev-sandwich-detector/`)
2. Write a `SKILL.md` following the format above
3. Focus on **teaching the AI agent** — explain the "why" alongside the "how"
4. Include anti-patterns and failure modes you've encountered

## License

Apache-2.0
