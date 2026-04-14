"""Per-pool configuration for V3 pools — allows the CE optimizer to evaluate
arbitrary pools (not just WETH/USDC on Base 0.3%).

Discovery flow:
  onchainos defi detail → underlyingToken addresses + feeRate
  + local TOKEN_DECIMALS lookup + FEE_TICK_SPACING rules
  → PoolConfig
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass

# Common ERC-20 decimals on Base / Arbitrum / Ethereum L1
TOKEN_DECIMALS = {
    # ETH family
    "ETH": 18, "WETH": 18, "CBETH": 18, "STETH": 18, "WSTETH": 18,
    "WEETH": 18, "EETH": 18, "RETH": 18, "OSETH": 18,
    # BTC family
    "WBTC": 8, "CBBTC": 8, "TBTC": 18,
    # Stables
    "USDC": 6, "USDT": 6, "DAI": 18, "USDG": 6, "FRAX": 18, "USDS": 18,
    "SDAI": 18, "CRVUSD": 18, "LUSD": 18, "PYUSD": 6, "TUSD": 18,
    # Native
    "OP": 18, "ARB": 18, "BNB": 18, "MATIC": 18, "POL": 18, "AVAX": 18,
}

# Uniswap V3 tick spacing per fee tier
FEE_TICK_SPACING = {
    0.0001: 1,     # 0.01%
    0.0005: 10,    # 0.05%
    0.003: 60,     # 0.3%
    0.01: 200,     # 1%
}

# Chain name → chainIndex
CHAIN_INDEX = {
    "base": "8453", "ethereum": "1", "arbitrum": "42161",
    "optimism": "10", "polygon": "137", "bsc": "56",
}


@dataclass
class PoolConfig:
    investment_id: str
    chain: str                   # "base", "arbitrum", ...
    chain_index: str             # "8453", ...
    token0_symbol: str           # lower-address token (V3 convention)
    token0_address: str
    token0_decimals: int
    token1_symbol: str           # higher-address token
    token1_address: str
    token1_decimals: int
    fee_tier: float              # 0.003, 0.0005, ...
    tick_spacing: int
    pool_address: str = ""       # may be empty if not easily parseable

    # ── Price/tick conversions ────────────────────────────────────────────
    #
    # V3 raw price = token1_units / token0_units (minimal units).
    # For WETH(t0,18d) × USDC(t1,6d): raw = USDC_μ / WETH_wei.
    # ETH price in USD = raw × 10^(d0 - d1).
    # But we usually think in "price of token X in USD". Depending on which
    # is the USD-denominated leg (the stable), we flip accordingly.
    #
    # For pair classification, we treat "display price" as "price of the
    # non-stable token in USD" when one side is a stable; for stable-stable
    # or bluechip-bluechip, we use raw.

    def display_price_from_tick(self, tick: int) -> float:
        """Return USD price of the non-stable leg (or raw ratio if neither stable).

        Logic: V3 raw = token1_base/token0_base. In human units,
        human_ratio = raw × 10^(d0-d1) = "token1 per token0".
          - c1 stable, c0 non-stable (e.g. WETH/USDC):
              human_ratio = USDC per WETH = USD per token0 → return directly
          - c0 stable, c1 non-stable (e.g. USDC/cbBTC):
              human_ratio = cbBTC per USDC. USD per token1 = 1 / human_ratio
          - otherwise return raw ratio
        """
        from token_registry import category
        raw = 1.0001 ** tick
        human_ratio = raw * (10 ** (self.token0_decimals - self.token1_decimals))
        c0 = category(self.token0_symbol)
        c1 = category(self.token1_symbol)
        if c1 == "stable" and c0 != "stable":
            return human_ratio
        if c0 == "stable" and c1 != "stable":
            return 1.0 / human_ratio if human_ratio > 0 else 0.0
        return human_ratio

    def display_price_to_tick(self, price: float) -> int:
        """Inverse of display_price_from_tick."""
        from token_registry import category
        c0 = category(self.token0_symbol)
        c1 = category(self.token1_symbol)
        if c1 == "stable" and c0 != "stable":
            human_ratio = price                              # USD per token0
        elif c0 == "stable" and c1 != "stable":
            human_ratio = 1.0 / price if price > 0 else 0.0  # token1 per stable
        else:
            human_ratio = price
        raw = human_ratio * (10 ** (self.token1_decimals - self.token0_decimals))
        return int(math.log(raw) / math.log(1.0001))

    def tick_to_price_usd(self, tick: int) -> float:
        """Alias for backward compat with capital_efficiency.py."""
        return self.display_price_from_tick(tick)

    def price_usd_to_tick(self, price: float) -> int:
        return self.display_price_to_tick(price)

    def sqrt_raw_price(self, display_price: float) -> float:
        """sqrt of raw pool price (raw = token1_base / token0_base)."""
        from token_registry import category
        c0 = category(self.token0_symbol)
        c1 = category(self.token1_symbol)
        if c1 == "stable" and c0 != "stable":
            human_ratio = display_price
        elif c0 == "stable" and c1 != "stable":
            human_ratio = 1.0 / display_price if display_price > 0 else 0.0
        else:
            human_ratio = display_price
        raw = human_ratio * (10 ** (self.token1_decimals - self.token0_decimals))
        return math.sqrt(raw) if raw > 0 else 0.0

    def calc_my_L(self, capital_usd: float, display_price: float,
                  lo_price: float, hi_price: float) -> float:
        """V3 L for a dual-token deposit of `capital_usd` into [lo,hi]
        at current `display_price`. Assumes token1 or token0 is stable
        (USD leg); for bluechip-bluechip pools we approximate using token1
        as the 'USD-equivalent' side.

        Returns L in V3 base units (comparable to depth chart liquidities).
        """
        sp = self.sqrt_raw_price(display_price)
        sa = self.sqrt_raw_price(lo_price)
        sb = self.sqrt_raw_price(hi_price)
        # Ensure sa < sb (needed when display_price inverts direction)
        if sa > sb:
            sa, sb = sb, sa

        # Clamp sqrt_p into range to handle edge cases
        if sp <= sa:
            # All capital in token0
            amt0 = capital_usd / display_price * (10 ** self.token0_decimals)
            return amt0 * sa * sb / (sb - sa)
        if sp >= sb:
            amt1 = capital_usd * (10 ** self.token1_decimals)
            return amt1 / (sb - sa)

        # In range: V3 formulas Δx = L (√Pb-√P)/(√Pb√P), Δy = L (√P-√Pa)
        x_per_L = (sb - sp) / (sb * sp)                 # token0 (raw units)
        y_per_L = (sp - sa)                              # token1 (raw units)
        # Convert per-L amounts to USD (using display_price as one leg's price)
        # If token1 is the stable, y_per_L is already USD×10^d1.
        # x_per_L × 10^(-d0) × display_price = USD
        from token_registry import category
        c0 = category(self.token0_symbol)
        c1 = category(self.token1_symbol)
        if c1 == "stable" and c0 != "stable":
            x_usd_per_L = x_per_L * display_price / (10 ** self.token0_decimals)
            y_usd_per_L = y_per_L / (10 ** self.token1_decimals)
        elif c0 == "stable" and c1 != "stable":
            x_usd_per_L = x_per_L / (10 ** self.token0_decimals)
            y_usd_per_L = y_per_L * display_price / (10 ** self.token1_decimals)
        else:
            # Neither side stable — use token1 as USD-equivalent (pool-denominated)
            x_usd_per_L = x_per_L * display_price / (10 ** self.token0_decimals)
            y_usd_per_L = y_per_L / (10 ** self.token1_decimals)

        total_usd_per_L = x_usd_per_L + y_usd_per_L
        return capital_usd / total_usd_per_L if total_usd_per_L > 0 else 0.0


def _run_onchainos(args: list[str]) -> dict:
    out = subprocess.check_output(["onchainos", *args], timeout=30)
    return json.loads(out)


def fetch_pool_config(investment_id, chain: str = "base") -> PoolConfig | None:
    """Build PoolConfig from onchainos defi detail."""
    investment_id = str(investment_id)
    try:
        r = _run_onchainos([
            "defi", "detail",
            "--investment-id", investment_id,
            "--chain", chain,
        ])
        d = r.get("data") or {}
    except Exception as e:
        print(f"fetch_pool_config failed for {investment_id}: {e}")
        return None

    tokens = d.get("underlyingToken") or []
    if len(tokens) != 2:
        return None

    # V3 convention: token0 is the lower-address token. underlyingToken order
    # may not reflect this, so sort by address.
    # BUT: "ETH" in OKX is represented as native 0xeeee... which isn't the
    # pool's actual token0 (would be WETH 0x4200...). Swap ETH→WETH for sort.
    WETH_BY_CHAIN = {
        "base": "0x4200000000000000000000000000000000000006",
        "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "optimism": "0x4200000000000000000000000000000000000006",
    }
    weth_addr = WETH_BY_CHAIN.get(chain, "").lower()
    toks = []
    for t in tokens:
        addr = (t.get("tokenAddress") or "").lower()
        sym = t.get("tokenSymbol") or "?"
        # Map native ETH to WETH for pool-level address comparison
        if sym == "ETH" and weth_addr:
            addr = weth_addr
            sym = "WETH"
        toks.append({"symbol": sym, "address": addr})
    # Sort ascending by address → token0, token1
    toks.sort(key=lambda x: x["address"])

    fee = float(d.get("feeRate") or 0)
    tick_spacing = FEE_TICK_SPACING.get(fee, 60)

    def _decimals(sym: str) -> int:
        return TOKEN_DECIMALS.get(sym.upper(), 18)

    return PoolConfig(
        investment_id=str(investment_id),
        chain=chain,
        chain_index=CHAIN_INDEX.get(chain, "8453"),
        token0_symbol=toks[0]["symbol"],
        token0_address=toks[0]["address"],
        token0_decimals=_decimals(toks[0]["symbol"]),
        token1_symbol=toks[1]["symbol"],
        token1_address=toks[1]["address"],
        token1_decimals=_decimals(toks[1]["symbol"]),
        fee_tier=fee,
        tick_spacing=tick_spacing,
    )


if __name__ == "__main__":
    import sys
    iid = sys.argv[1] if len(sys.argv) > 1 else "326890603"
    chain = sys.argv[2] if len(sys.argv) > 2 else "base"
    cfg = fetch_pool_config(iid, chain)
    if not cfg:
        print("failed")
        sys.exit(1)
    print(f"{cfg.token0_symbol}/{cfg.token1_symbol} fee={cfg.fee_tier} "
          f"tick_sp={cfg.tick_spacing}")
    print(f"  token0: {cfg.token0_address} ({cfg.token0_decimals}d)")
    print(f"  token1: {cfg.token1_address} ({cfg.token1_decimals}d)")
    # Sanity: tick → price roundtrip
    for p in [2000, 2400, 2800]:
        t = cfg.price_usd_to_tick(p)
        p2 = cfg.tick_to_price_usd(t)
        print(f"  price ${p} → tick {t} → ${p2:.2f}")
