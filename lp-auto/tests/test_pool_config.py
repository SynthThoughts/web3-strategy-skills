"""Unit tests for PoolConfig math — price↔tick roundtrip, calc_my_L."""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "references"))

from pool_config import PoolConfig, FEE_TICK_SPACING, TOKEN_DECIMALS


def _make_cfg(token0_sym="WETH", token0_dec=18,
              token1_sym="USDC", token1_dec=6,
              fee=0.003):
    return PoolConfig(
        investment_id="test",
        chain="base", chain_index="8453",
        token0_symbol=token0_sym,
        token0_address="0x" + "0" * 40,
        token0_decimals=token0_dec,
        token1_symbol=token1_sym,
        token1_address="0x" + "f" * 40,
        token1_decimals=token1_dec,
        fee_tier=fee,
        tick_spacing=FEE_TICK_SPACING.get(fee, 60),
    )


class TestRoundtrip:
    def test_weth_usdc(self):
        cfg = _make_cfg("WETH", 18, "USDC", 6, 0.003)
        for p in [1500.0, 2000.0, 2400.0, 3000.0, 4500.0]:
            t = cfg.price_usd_to_tick(p)
            p2 = cfg.tick_to_price_usd(t)
            err = abs(p - p2) / p
            assert err < 0.001, f"price ${p} roundtrip err {err*100:.3f}%"

    def test_usdc_cbbtc(self):
        """Stable as token0, bluechip as token1 — inverse price direction."""
        cfg = _make_cfg("USDC", 6, "cbBTC", 8, 0.0005)
        for p in [40000.0, 60000.0, 80000.0, 100000.0]:
            t = cfg.price_usd_to_tick(p)
            p2 = cfg.tick_to_price_usd(t)
            err = abs(p - p2) / p
            assert err < 0.001, f"BTC price ${p} roundtrip err {err*100:.3f}%"

    def test_known_eth_tick(self):
        """ETH @ $2400 on WETH/USDC pool should produce tick ≈ -198487."""
        cfg = _make_cfg("WETH", 18, "USDC", 6, 0.003)
        t = cfg.price_usd_to_tick(2400.0)
        # Real pool tick for $2400 is around -198449..-198487 depending on rounding
        assert -198600 < t < -198400, f"expected ~-198487, got {t}"


class TestCalcMyL:
    def test_balanced_deposit(self):
        """In-range deposit should produce positive L value comparable to depth chart units."""
        cfg = _make_cfg("WETH", 18, "USDC", 6, 0.003)
        L = cfg.calc_my_L(400.0, 2400.0, 2300.0, 2500.0)
        assert L > 0
        # Expected order: 1e14-1e15 for $400 LP in WETH/USDC with 8% band
        assert 1e13 < L < 1e16, f"L out of expected range: {L:.2e}"

    def test_narrow_range_higher_L(self):
        """Narrower range at same capital → more L (concentration)."""
        cfg = _make_cfg("WETH", 18, "USDC", 6, 0.003)
        L_wide = cfg.calc_my_L(400.0, 2400.0, 2200.0, 2600.0)   # 8% wide
        L_narrow = cfg.calc_my_L(400.0, 2400.0, 2380.0, 2420.0) # 1% wide
        assert L_narrow > L_wide, f"narrow L={L_narrow:.2e} should exceed wide L={L_wide:.2e}"

    def test_above_range_all_token1(self):
        """If price > hi, all capital in token1 (stable)."""
        cfg = _make_cfg("WETH", 18, "USDC", 6, 0.003)
        L = cfg.calc_my_L(400.0, 3000.0, 2200.0, 2600.0)   # price above range
        assert L > 0

    def test_below_range_all_token0(self):
        cfg = _make_cfg("WETH", 18, "USDC", 6, 0.003)
        L = cfg.calc_my_L(400.0, 2000.0, 2200.0, 2600.0)   # price below range
        assert L > 0


if __name__ == "__main__":
    failed = 0
    for cls in [TestRoundtrip, TestCalcMyL]:
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
