#!/usr/bin/env python3
"""Rank candidate V3 pools by risk tier + live APY + TVL.

Uses `onchainos defi search` to discover pools, then classifies each pair
(token_a × token_b) into a risk tier via token_registry.risk_tier().
Filters by max_risk (default "medium") and sorts by live APY.

This is a *discovery* tool only — does not execute any on-chain action.
Use it to decide whether to switch pools; actual switch still manual.

Usage:
  pool_compare.py [--chain base] [--max-risk medium] [--tokens USDC,ETH,BTC]

Output: a ranked table grouped by risk tier.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from token_registry import risk_tier, tier_rank, allowed, normalize


PAIR_RE = re.compile(r"([A-Za-z0-9.]+)[-/]([A-Za-z0-9.]+)")


def parse_pair(name: str) -> tuple[str, str] | None:
    m = PAIR_RE.search(name or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def search_pools(chain: str, token_filter: list[str] | None = None,
                 max_pages: int = 3) -> list[dict]:
    """Discover DEX V3 pools via onchainos defi search.

    onchainos --token is single-term AND matching, so we loop one token at a
    time and de-dup by investmentId.
    """
    all_pools: dict[str, dict] = {}
    tokens = token_filter or ["USDC", "ETH", "BTC"]
    for tok in tokens:
        for page in range(1, max_pages + 1):
            args = [
                "onchainos", "defi", "search",
                "--chain", chain,
                "--token", tok,
                "--product-group", "DEX_POOL",
                "--page-num", str(page),
            ]
            try:
                out = subprocess.check_output(args, timeout=30)
                data = json.loads(out).get("data", {}) or {}
            except Exception as e:
                print(f"  search [{tok}] p{page} failed: {e}")
                break
            page_items = data.get("list") or data.get("items") or []
            if not page_items:
                break
            for it in page_items:
                iid = it.get("investmentId")
                if iid and iid not in all_pools:
                    all_pools[iid] = it
            if len(page_items) < 20:
                break
    return list(all_pools.values())


def score(pool: dict) -> dict:
    """Enrich pool dict with parsed pair + risk tier."""
    name = pool.get("name", "")
    pair = parse_pair(name)
    out = {
        "id": pool.get("investmentId", "?"),
        "name": name,
        "platform": pool.get("platformName", ""),
        "tvl": float(pool.get("tvl") or 0),
        "apy": float(pool.get("rate") or 0),
        "fee": float(pool.get("feeRate") or 0),
        "tier": "unknown",
        "pair": "",
    }
    if pair:
        a, b = pair
        out["pair"] = f"{normalize(a)}-{normalize(b)}"
        out["tier"] = risk_tier(a, b)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="base")
    ap.add_argument("--max-risk", default="medium",
                    help="very-low|low|medium|medium-high|high|very-high")
    ap.add_argument("--tokens", default="USDC,ETH,BTC,DAI,USDT",
                    help="comma-separated search seed tokens")
    ap.add_argument("--min-tvl", type=float, default=100_000.0)
    ap.add_argument("--min-apy", type=float, default=0.0,
                    help="minimum APY as decimal (0.10 = 10%%)")
    ap.add_argument("--show-all", action="store_true",
                    help="also show filtered-out pools (for debug)")
    args = ap.parse_args()

    token_filter = args.tokens.split(",")
    print(f"Searching {args.chain} for [{args.tokens}] DEX pools...")
    raw = search_pools(args.chain, token_filter)
    print(f"  found {len(raw)} pools")
    if not raw:
        return

    enriched = [score(p) for p in raw]
    # de-dup by investmentId
    seen = set()
    uniq = []
    for p in enriched:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        uniq.append(p)

    # Partition: allowed by risk + passed filters vs rejected
    passed, rejected = [], []
    for p in uniq:
        reasons = []
        if not allowed(p["tier"], args.max_risk):
            reasons.append(f"risk={p['tier']}")
        if p["tvl"] < args.min_tvl:
            reasons.append(f"tvl=${p['tvl']:,.0f}")
        if p["apy"] < args.min_apy:
            reasons.append(f"apy={p['apy']*100:.1f}%")
        if reasons:
            p["_reasons"] = "; ".join(reasons)
            rejected.append(p)
        else:
            passed.append(p)

    # Rank allowed pools: sort by tier (lower risk first), then APY desc
    passed.sort(key=lambda x: (tier_rank(x["tier"]), -x["apy"]))

    print()
    print(f"== PASSED ({len(passed)})  max_risk={args.max_risk} min_tvl=${args.min_tvl:,.0f} min_apy={args.min_apy*100:.0f}%")
    print(f"{'id':>10} {'pair':>14} {'tier':<13} {'TVL':>14} {'APY':>7} {'fee':>6} {'platform':<12}")
    for p in passed:
        print(f"{p['id']:>10} {p['pair']:>14} {p['tier']:<13} ${p['tvl']:>12,.0f} {p['apy']*100:>6.2f}% {p['fee']*100:>5.2f}% {p['platform'][:12]:<12}")

    if args.show_all and rejected:
        print()
        print(f"== REJECTED ({len(rejected)})")
        rejected.sort(key=lambda x: -x["apy"])
        for p in rejected[:20]:
            print(f"{p['id']:>10} {p['pair']:>14} {p['tier']:<13} ${p['tvl']:>12,.0f} {p['apy']*100:>6.2f}%  ({p.get('_reasons', '')})")


if __name__ == "__main__":
    main()
