---
name: polymarket-arb-scanner
description: "Prediction market arbitrage detection on Polymarket CLOB. Three-layer framework: single-condition (YES+NO mispricing), neg-risk multi-outcome (∑YES mispricing), and cross-market implication (logical dependency violations). Covers CLOB orderbook fetching, false-positive filtering (overround, cold markets, resolved markets, direction traps), and cross-market implication pattern matching (price thresholds, deadline nesting). Use when building, debugging, or extending a Polymarket arb scanner."
license: Apache-2.0
metadata:
  author: SynthThoughts
  version: "1.0.0"
---

# Polymarket Arbitrage Scanner

Three-layer arbitrage detection framework for Polymarket's CLOB (Central Limit Order Book).

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

## API Endpoints

| API | URL | Purpose |
|-----|-----|---------|
| Gamma | `https://gamma-api.polymarket.com` | Market metadata (events, conditions, slugs) |
| CLOB | `https://clob.polymarket.com` | Real-time orderbook (bid/ask/depth) |
| CLOB WS | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Streaming price updates |

**Critical**: Gamma API `outcomePrices` are severely inaccurate. Example: NCAA tournament showed ∑bid=14.18 in Gamma vs 0.41 in CLOB. **Always use CLOB as price source.**

## CLOB Orderbook Fetching

```typescript
// GET https://clob.polymarket.com/book?token_id={tokenId}
// Response: { bids: [{price, size}], asks: [{price, size}] }

interface OrderbookResult {
  bestBid: number;      // max(bids.price)
  bestAsk: number;      // min(asks.price)
  bidDepthUsd: number;  // Σ(bid.price × bid.size)
  askDepthUsd: number;  // Σ(ask.price × ask.size)
}
```

Rate limiting: ~10 req/sec safe, add 500ms pause every 10 requests.

## Layer 1a: Single-Condition Arbitrage

Each Polymarket condition has YES and NO tokens. Prices should satisfy: `ask(YES) + ask(NO) ≥ 1` and `bid(YES) + bid(NO) ≤ 1`.

### BUY_BOTH

```
Condition:  ask(YES) + ask(NO) < 1.0 - TOLERANCE
Profit:     1.0 - (ask_YES + ask_NO)  per dollar
Depth:      min(askDepth_YES, askDepth_NO)
Action:     Buy both YES and NO tokens → guaranteed $1 payout
```

### SPLIT_SELL

```
Condition:  bid(YES) + bid(NO) > 1.0 + TOLERANCE
Profit:     (bid_YES + bid_NO) - 1.0  per dollar
Depth:      min(bidDepth_YES, bidDepth_NO)
Action:     Mint YES+NO for $1 (via CTF), sell both at market
```

Only check non-neg-risk markets (neg-risk handled in Layer 1b).

## Layer 1b: Neg-Risk Multi-Outcome Arbitrage

Neg-risk events (e.g., "2028 Presidential Election") have mutually exclusive outcomes where exactly one YES wins. Constraint: `∑P(outcome_i) = 1`.

### BUY_ALL_YES

```
Condition:  ∑ask(YES_i) < 1.0 - TOLERANCE
Profit:     1.0 - ∑ask(YES_i)  per dollar
Depth:      min(askDepth_i)
Action:     Buy all YES outcomes → one must pay $1
```

### SELL_ALL_YES

```
Condition:  ∑bid(YES_i) > 1.0 + TOLERANCE + overround
Overround:  min(numOutcomes × 0.03, 1.5)
Net profit: (∑bid - 1.0) - overround  per dollar
Depth:      min(bidDepth_i)
Action:     Sell all YES outcomes → at most one costs $1
```

**Overround (vig)**: Market makers naturally price ∑YES > 1 for profit. ~3% per outcome is expected. Only flag when excess exceeds this.

## Layer 2: Cross-Market Implication Arbitrage

If event A logically implies event B (A→B), then P(A) ≤ P(B). If CLOB shows `bid(A) > ask(B)`, that's arbitrage.

### Implication Pattern Detection

#### Pattern 1: Price Threshold (same asset, same timeframe, same direction)

```
"reach" direction (up):
  Higher threshold → lower threshold  (stronger = higher)
  "BTC reach $150k by Dec 2026" → "BTC reach $100k by Dec 2026"

"dip to" direction (down):  ⚠️ REVERSED!
  Lower threshold → higher threshold  (stronger = lower)
  "BTC dip to $5k by Dec 2026" → "BTC dip to $55k by Dec 2026"
```

**Strict requirements:**
- Same asset (BTC or ETH)
- Same direction (both "reach" or both "dip")
- Same timeframe (both "by Dec 2026", not mixing "by Dec 2026" with "on March 12")
- Asset name must appear in price context (reject "MicroStrategy 800k BTC", "S&P 500 buys Bitcoin")
- Threshold must be plausible (BTC: $100–$10M, ETH: $10–$1M)

#### Pattern 2: Deadline Nesting (same topic, different deadlines)

```
Earlier deadline → later deadline  (stronger = earlier)
"X by March 31, 2026" → "X by December 31, 2026"
```

**Strict requirements:**
- Topic text must match exactly (after normalization)
- Exclude independent periodic events (Fed rate meetings at different dates are INDEPENDENT, not implications)

### Implication Arbitrage Detection

```
Given: A → B (A is "stronger", B is "weaker")
Normal:    P(A) ≤ P(B), i.e., ask(A) < bid(B) is expected
Violation: bid(A) > ask(B) + 0.01
Action:    SELL A at bid(A), BUY B at ask(B)
Profit:    bid(A) - ask(B) per dollar
Depth:     min(bidDepth_A, askDepth_B)
```

#### Timeframe Parsing

```
"by December 31, 2026"  → by-december-2026
"in March"               → in-march
"on March 12"            → on-march-12     (daily, distinct dates!)
"March 9-15"             → week-march-9-15
```

Daily markets on different dates (e.g., "on March 12" vs "on March 13") are NOT the same timeframe — do not compare.

## False-Positive Filters

| Filter | Check | Eliminates |
|--------|-------|-----------|
| Resolved market | `ask=0 && bid≥0.99` | Expired markets with no sellers |
| No depth | `depth < $50 both sides` | Untradeable illiquid markets |
| Cold market | `∑YES < 0.10` | Dead/abandoned markets |
| Low volume | `volume < $1,000` | Noise markets |
| Direction mismatch | `direction !== direction` | Confusing "reach" with "dip" |
| Timeframe mismatch | `timeframe !== timeframe` | Mixing daily with yearly |
| Non-price number | threshold sanity check | "S&P 500" matched as "$500" |
| Asset context | strict regex | "MicroStrategy 800k BTC" matched as "BTC $800k" |
| Independent events | Fed meeting filter | Different Fed meetings are not implications |
| Expected overround | `n × 0.03` per outcome | Normal market maker vig |

### The "Dip To" Direction Trap

This is the most subtle false positive. "Will BTC dip to $X" means price falls TO $X, i.e., price ≤ $X.

```
✅ Correct:  dip to $5k  → dip to $55k  (if it fell to $5k, it passed $55k)
❌ Wrong:    dip to $55k → dip to $5k   (falling to $55k doesn't mean falling to $5k)
```

The implication direction for "dip" is OPPOSITE to "reach":
- reach: higher implies lower (stronger = higher threshold)
- dip: lower implies higher (stronger = lower threshold)

## Polymarket Contract Architecture

| Contract | Address | Purpose |
|----------|---------|---------|
| CTF | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | Conditional Token Framework |
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` | Non-neg-risk trading |
| Neg-Risk Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` | Neg-risk trading |
| Neg-Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` | Neg-risk token conversion |
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | Collateral (Polygon) |

## Execution Flow

```
# Layer 1: One-shot scan
1. Load all active events from Gamma API (paginated, ~8000 events)
2. Filter: active, not closed, volume ≥ $1k, has clobTokenIds
3. For non-neg-risk: fetch YES+NO orderbooks, check BUY_BOTH / SPLIT_SELL
4. For neg-risk events: fetch all YES orderbooks, check BUY_ALL_YES / SELL_ALL_YES
5. Apply quality filters, sort by maxProfit

# Layer 2: Cross-market scan
1. Load high-volume markets (volume ≥ $50k) from Gamma API
2. O(n²) pairwise comparison: parsePriceMarket() + parseDeadlineMarket()
3. For each implication pair: fetch both CLOB orderbooks
4. Check if bid(stronger) > ask(weaker) + tolerance
5. Filter resolved markets, minimum depth, sort by maxProfit
```

## Empirical Results

Scanning Polymarket (March 2026):

| Layer | Markets Scanned | Arbs Found |
|-------|----------------|------------|
| 1a Single-condition | 2,859 | **0** |
| 1b Neg-risk events | 20+ events | **0** |
| 2 Cross-market | 521 implication pairs | **0** |

**Conclusion**: Polymarket's CLOB is efficiently priced. Market makers maintain correct pricing both within markets and across logically related markets.

## Extending This Framework

### Adding New Implication Patterns

```typescript
// In detectImplication(), add new pattern matchers:

// Political: candidate win → party win
// "Trump wins Pennsylvania" → "Republican wins Pennsylvania"

// Sports: team wins series → team advances
// "Lakers win Round 1" → "Lakers make Conference Finals"

// Composite: use LLM to detect semantic implications
// Feed market question pairs to Claude for relationship classification
```

### Real-Time Monitoring

```typescript
// Replace one-shot CLOB fetch with WebSocket streaming:
const ws = new WebSocket('wss://ws-subscriptions-clob.polymarket.com/ws/market');
ws.send(JSON.stringify({ type: 'subscribe', channel: 'book', assets_id: tokenId }));
// React to orderbook updates → recheck arb conditions in <100ms
```

### Auto-Execution

```
1. Detect arb via CLOB
2. Estimate gas + slippage
3. If net profit > threshold:
   a. For BUY_BOTH: buy YES + buy NO via CTF Exchange
   b. For SPLIT_SELL: splitPosition() via CTF, then sell both
   c. For cross-market: sell stronger + buy weaker (two separate trades)
4. Monitor fill, adjust if partial
```
