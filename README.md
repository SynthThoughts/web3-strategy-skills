# Web3-Skills

Reusable Web3 trading skills for AI coding agents (Claude Code, Cursor, etc.).

Each skill is a self-contained `SKILL.md` that teaches an AI agent how to build, debug, and extend a specific trading strategy — covering architecture, algorithms, parameters, risk controls, and anti-patterns.

## Skills

| Skill | Description |
|-------|-------------|
| [grid-trading](./grid-trading/) | Dynamic grid trading on EVM L2 chains via OKX DEX API. Volatility-adaptive step sizing, position limits, circuit breakers. |
| [polymarket-arb-scanner](./polymarket-arb-scanner/) | Three-layer arbitrage detection on Polymarket CLOB: single-condition, neg-risk multi-outcome, and cross-market logical implication. |

## How to Use

### Claude Code

Add a skill to your project by copying its folder into your workspace, or reference it in your `.claude/settings.json`:

```bash
# Copy a skill into your project
cp -r grid-trading /path/to/your/project/.claude/skills/
```

### Other AI Agents

Each `SKILL.md` is plain Markdown — paste it into any AI agent's system prompt or context window. No proprietary format required.

## Skill Format

```
skill-name/
└── SKILL.md     # Complete knowledge document with YAML frontmatter
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
