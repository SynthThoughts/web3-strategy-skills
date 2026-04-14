"""Unit tests for token classifier + pair risk-tier mapping."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "references"))

import token_registry as tr


class TestCategory:
    def test_bluechip(self):
        for s in ["ETH", "WETH", "BTC", "WBTC", "cbBTC", "cbETH", "tBTC"]:
            assert tr.category(s) == "bluechip", s

    def test_lst(self):
        for s in ["stETH", "wstETH", "weETH", "rETH", "osETH", "ETHx"]:
            assert tr.category(s) == "lst", s

    def test_stable(self):
        for s in ["USDC", "USDT", "DAI", "FRAX", "USDS", "crvUSD", "sUSDe"]:
            assert tr.category(s) == "stable", s

    def test_native(self):
        for s in ["OP", "ARB", "BNB", "SOL", "POL", "AVAX"]:
            assert tr.category(s) == "native", s

    def test_other_fallback(self):
        for s in ["PEPE", "LINK", "UNI", "AAVE", "XYZ_UNKNOWN"]:
            assert tr.category(s) == "other", s

    def test_normalize(self):
        assert tr.normalize("usdc") == "USDC"
        assert tr.normalize("USDC.e") == "USDC"
        assert tr.normalize("weth.base") == "WETH"
        assert tr.category("usdc.e") == "stable"


class TestRiskTier:
    def test_stable_stable(self):
        assert tr.risk_tier("USDC", "USDT") == "very-low"
        assert tr.risk_tier("USDC", "DAI") == "very-low"
        assert tr.risk_tier("USDC.e", "USDC") == "very-low"

    def test_pure_wrap(self):
        assert tr.risk_tier("ETH", "WETH") == "very-low"
        assert tr.risk_tier("WETH", "cbETH") == "very-low"
        assert tr.risk_tier("WBTC", "cbBTC") == "very-low"

    def test_bluechip_lst(self):
        assert tr.risk_tier("ETH", "stETH") == "low"
        assert tr.risk_tier("WETH", "weETH") == "low"
        assert tr.risk_tier("ETH", "rETH") == "low"

    def test_bluechip_stable(self):
        assert tr.risk_tier("ETH", "USDC") == "medium"
        assert tr.risk_tier("WBTC", "USDC") == "medium"
        assert tr.risk_tier("cbBTC", "DAI") == "medium"
        # LST × stable also medium (LST treated as bluechip for pool purpose)
        assert tr.risk_tier("stETH", "USDC") == "medium"

    def test_bluechip_bluechip(self):
        assert tr.risk_tier("ETH", "WBTC") == "medium-high"
        assert tr.risk_tier("WETH", "cbBTC") == "medium-high"

    def test_native_with_bluechip_or_stable(self):
        assert tr.risk_tier("OP", "USDC") == "high"
        assert tr.risk_tier("ARB", "ETH") == "high"
        assert tr.risk_tier("BNB", "USDC") == "high"

    def test_very_high(self):
        assert tr.risk_tier("OP", "ARB") == "very-high"       # native × native
        assert tr.risk_tier("PEPE", "USDC") == "very-high"    # other × stable
        assert tr.risk_tier("ETH", "PEPE") == "very-high"     # bluechip × other
        assert tr.risk_tier("LINK", "AAVE") == "very-high"    # other × other

    def test_tier_rank_ordering(self):
        tiers = ["very-low", "low", "medium", "medium-high", "high", "very-high"]
        ranks = [tr.tier_rank(t) for t in tiers]
        assert ranks == sorted(ranks), "tier ranks must be monotone ascending"


class TestAllowed:
    def test_at_limit(self):
        assert tr.allowed("medium", "medium") is True

    def test_below_limit(self):
        assert tr.allowed("very-low", "medium") is True
        assert tr.allowed("low", "medium") is True

    def test_above_limit(self):
        assert tr.allowed("high", "medium") is False
        assert tr.allowed("very-high", "medium") is False

    def test_default_ceiling(self):
        # Default max_tier is medium; very-high always rejected
        assert tr.allowed("very-high") is False


if __name__ == "__main__":
    # Simple runner for environments without pytest
    failed = 0
    for cls in [TestCategory, TestRiskTier, TestAllowed]:
        inst = cls()
        for name in dir(inst):
            if not name.startswith("test_"):
                continue
            try:
                getattr(inst, name)()
                print(f"  ✓ {cls.__name__}.{name}")
            except AssertionError as e:
                print(f"  ✗ {cls.__name__}.{name}: {e}")
                failed += 1
    print(f"\n{'FAILED' if failed else 'PASSED'} — {failed} failures")
    sys.exit(0 if failed == 0 else 1)
