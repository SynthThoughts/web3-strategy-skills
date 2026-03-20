# Polymarket Arb Scanner

Three-layer arbitrage detection framework for Polymarket's CLOB (Central Limit Order Book).

## Features

- **Layer 1a: Single-condition arb** — YES + NO mispricing on individual markets
- **Layer 1b: Neg-risk multi-outcome arb** — sum-of-YES mispricing across multi-outcome events
- **Layer 2: Cross-market implication arb** — logical dependency violations between related markets
- **False-positive filtering** — overround checks, cold market detection, resolved market filtering, direction traps
- **CLOB-based pricing** — always uses real orderbook data, never inaccurate Gamma API prices
- **Depth-aware profit calculation** — accounts for actual liquidity at each price level

## Architecture

```
Gamma API (metadata)  →  MarketStore (filter active, high-volume)
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        Layer 1a           Layer 1b          Layer 2
     Single-condition    Neg-risk event    Cross-market
      YES/NO arb         ∑YES arb        implication arb
              │                │                │
              ▼                ▼                ▼
           CLOB API ←──────────────────────→ CLOB API
        (orderbook)                       (orderbook)
              │                │                │
              ▼                ▼                ▼
        Quality filters   Quality filters   False-positive
                                             filters
              │                │                │
              └────────────────┼────────────────┘
                               ▼
                      Sorted results by maxProfit
```

## Installation

**ClawHub** (recommended):
```bash
npx clawhub install polymarket-arb-scanner
```

**Manual**:
```bash
cp -r polymarket-arb-scanner ~/.openclaw/skills/
```

## Directory Structure

```
polymarket-arb-scanner/
└── SKILL.md    # Core knowledge: arb layers, CLOB fetching, filters, patterns
```

## Prerequisites

- Python 3.10+ or Node.js 18+
- Network access to Polymarket APIs (Gamma + CLOB)
- No API key required (public endpoints)

## License

Apache-2.0
