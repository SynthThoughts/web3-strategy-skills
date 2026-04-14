"""Token classification + pair risk-tier mapping.

SPEC: references/token-risk-classification.md is the source of truth.
Any change here must be mirrored in that doc (and vice versa).

Quick reference:
- bluechip : ETH, BTC + pure wraps (WETH, cbETH, WBTC, cbBTC, tBTC)
- lst      : ETH-tracking LSTs (stETH, weETH, rETH, ...)
- stable   : USD-pegged (USDC, USDT, DAI, ...)
- native   : L1/L2 gas tokens (OP, ARB, BNB, ...; ETH/BTC excluded)
- other    : fail-safe bucket (long-tail, memes, DeFi blue chips)

Tiers (ascending): very-low < low < medium < medium-high < high < very-high
"""
from __future__ import annotations

BLUECHIP = {
    # ETH family (1:1 or near-1:1 to ETH)
    "ETH", "WETH", "CBETH",
    # BTC family
    "BTC", "WBTC", "CBBTC", "TBTC",
}

# Liquid staking tokens — tracked against ETH but carry separate risk
LST = {
    "STETH", "WSTETH",        # Lido
    "WEETH", "EETH",          # EtherFi
    "RETH",                   # Rocket Pool
    "OSETH",                  # Stakewise
    "ETHX",                   # Stader
    "SWETH",                  # Swell
    "ANKRETH",                # Ankr
}

STABLE = {
    "USDC", "USDT", "USDG", "DAI", "FRAX", "USDS",
    "SDAI", "CRVUSD", "LUSD", "GUSD", "PYUSD", "TUSD",
    "MIM", "FDUSD", "USDE", "SUSDE",
}

# L1/L2 gas/governance tokens (ETH/BTC excluded — they're bluechip)
NATIVE = {
    "OP", "ARB", "BNB", "MATIC", "POL", "AVAX", "SOL",
    "S",    # Sonic
    "SUI", "APT", "TRX", "TON", "NEAR", "FTM",
    "CRO", "CELO",
}

# Pure-wrap groups: within a group, tokens are ~1:1 (very-low risk pair)
_PURE_WRAP_GROUPS: list[set[str]] = [
    {"ETH", "WETH", "CBETH"},
    {"BTC", "WBTC", "CBBTC", "TBTC"},
]

# LST + bluechip-ETH group: pair between LST and ETH-wrap = low risk
_ETH_LST_GROUP: set[str] = {
    "ETH", "WETH",
    "STETH", "WSTETH", "WEETH", "EETH", "RETH",
    "OSETH", "ETHX", "SWETH", "ANKRETH",
}

RISK_TIERS = ["very-low", "low", "medium", "medium-high", "high", "very-high"]
_TIER_RANK = {t: i for i, t in enumerate(RISK_TIERS)}


def normalize(sym: str) -> str:
    """Uppercase + strip common bridge/network suffixes (USDC.e → USDC)."""
    s = sym.upper().strip()
    for suf in (".E", ".BASE", ".ARB", ".OP", ".POLY"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def category(sym: str) -> str:
    s = normalize(sym)
    if s in BLUECHIP:
        return "bluechip"
    if s in LST:
        return "lst"
    if s in STABLE:
        return "stable"
    if s in NATIVE:
        return "native"
    return "other"


def _same_pure_wrap(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    pair = {a, b}
    return any(pair <= g for g in _PURE_WRAP_GROUPS)


def _bluechip_lst_pair(a: str, b: str) -> bool:
    """True if pair is bluechip-ETH × LST (e.g. ETH-stETH, WETH-weETH)."""
    a, b = normalize(a), normalize(b)
    pair = {a, b}
    # Both must be in ETH_LST_GROUP, and at least one must be an LST
    return pair <= _ETH_LST_GROUP and bool(pair & LST)


def risk_tier(token_a: str, token_b: str) -> str:
    """Return risk tier string for a token pair."""
    ca, cb = category(token_a), category(token_b)
    cats = {ca, cb}

    if _same_pure_wrap(token_a, token_b):
        return "very-low"

    if ca == "stable" and cb == "stable":
        return "very-low"

    if _bluechip_lst_pair(token_a, token_b):
        return "low"

    # Treat LST paired with a stable like bluechip×stable (medium); LST alone
    # (no ETH on the other side) gets medium-high. Rarely seen in practice.
    if cats == {"bluechip", "stable"} or cats == {"lst", "stable"}:
        return "medium"

    if ca == "bluechip" and cb == "bluechip":
        return "medium-high"

    if cats == {"lst", "lst"} or cats == {"bluechip", "lst"}:
        # bluechip×LST without same-group (e.g. BTC-stETH — exotic) or LST-LST
        return "medium-high"

    if "native" in cats and cats & {"stable", "bluechip"}:
        return "high"

    # native × native, anything × other, native × other
    return "very-high"


def tier_rank(tier: str) -> int:
    return _TIER_RANK.get(tier, 99)


def allowed(tier: str, max_tier: str = "medium") -> bool:
    """True if `tier` is at or below `max_tier` (inclusive)."""
    return tier_rank(tier) <= tier_rank(max_tier)


# ── Self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cases = [
        ("USDC", "USDT",    "very-low",    "stable × stable"),
        ("USDC", "DAI",     "very-low",    "stable × stable"),
        ("ETH",  "WETH",    "very-low",    "pure wrap"),
        ("WBTC", "cbBTC",   "very-low",    "pure wrap (BTC family)"),
        ("ETH",  "stETH",   "low",         "bluechip × LST"),
        ("WETH", "weETH",   "low",         "bluechip × LST"),
        ("ETH",  "USDC",    "medium",      "bluechip × stable"),
        ("WBTC", "USDC",    "medium",      "bluechip × stable"),
        ("stETH","USDC",    "medium",      "LST × stable (treated as bluechip)"),
        ("ETH",  "WBTC",    "medium-high", "bluechip × bluechip (diff asset)"),
        ("OP",   "USDC",    "high",        "native × stable"),
        ("ARB",  "ETH",     "high",        "native × bluechip"),
        ("OP",   "ARB",     "very-high",   "native × native"),
        ("PEPE", "USDC",    "very-high",   "other × stable"),
        ("ETH",  "PEPE",    "very-high",   "bluechip × other"),
        ("USDC.e", "USDC",  "very-low",    "normalize + stable×stable"),
    ]
    ok = 0
    for a, b, expected, label in cases:
        got = risk_tier(a, b)
        status = "✓" if got == expected else "✗"
        if got == expected:
            ok += 1
        print(f"  {status} {a:>8} × {b:<8} → {got:<12} (expect {expected:<12}) — {label}")
    print(f"\n{ok}/{len(cases)} passed")
