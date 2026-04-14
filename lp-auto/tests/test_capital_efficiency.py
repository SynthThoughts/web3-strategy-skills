"""Unit tests for capital_efficiency scoring helpers (pure math, no I/O)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "references"))

import capital_efficiency as ce


class TestTickPriceRoundtrip:
    """capital_efficiency has its own price/tick helpers for WETH/USDC default."""

    def test_roundtrip(self):
        for p in [2000.0, 2400.0, 3000.0]:
            t = ce.price_usd_to_tick(p)
            p2 = ce.tick_to_price_usd(t)
            err = abs(p - p2) / p
            assert err < 0.01, f"err {err*100:.2f}% on ${p}"


class TestAvgLInRange:
    def test_single_sample_in_range(self):
        depth = [(100, 5)]
        assert ce.avg_L_in_range(depth, 50, 150) == 5.0

    def test_multiple_samples_tick_weighted(self):
        depth = [(0, 10), (100, 20), (200, 30)]
        # Mean: ((10 × 100) + (20 × 100)) / 200 = 15 — tick-weighted average
        val = ce.avg_L_in_range(depth, 0, 200)
        assert 14 < val < 16, f"expected ~15, got {val}"

    def test_empty(self):
        assert ce.avg_L_in_range([], 0, 100) == 0.0

    def test_bracket_inserts_below_above_samples(self):
        """Range edges are filled by surrounding samples' L values."""
        depth = [(0, 10), (500, 50)]
        val = ce.avg_L_in_range(depth, 100, 400)
        # Brackets into: [(100, 10 from below), (400, 50 from above)]
        # span 300, weighted avg = 10 (since only one sample per span)
        assert val > 0


class TestTimeInRange:
    def test_empty(self):
        assert ce.time_in_range([], 100, 200) == 0.0

    def test_all_in(self):
        prices = [(i * 1000, 150.0) for i in range(24)]
        assert ce.time_in_range(prices, 100, 200) == 1.0

    def test_none_in(self):
        prices = [(i * 1000, 50.0) for i in range(24)]
        assert ce.time_in_range(prices, 100, 200) == 0.0

    def test_half_in(self):
        prices = [(i * 1000, 150.0 if i < 12 else 250.0) for i in range(24)]
        assert ce.time_in_range(prices, 100, 200) == 0.5


class TestRecentApy:
    def test_last_3h_mean(self):
        rates = [(i * 3600000, 0.1 + i * 0.1, 100.0) for i in range(10)]
        # last 3: (0.8, 0.9, 1.0) rates, mean = 0.9
        assert abs(ce.recent_apy(rates, 3) - 0.9) < 0.01

    def test_fewer_than_window(self):
        rates = [(0, 0.5, 10.0)]
        assert ce.recent_apy(rates, 3) == 0.5

    def test_empty(self):
        assert ce.recent_apy([]) == 0.0


class TestExpectedRebalances:
    def test_tight_path_few_rebals(self):
        # Price path barely moves; narrow range width
        prices = [(i * 3600000, 2000.0 + i * 0.5) for i in range(24)]
        n = ce.expected_rebalances_24h(prices, 1990.0, 2020.0)
        # Total path ~ 24 * 0.5 = 12. Width = 30. n ≈ 0
        assert n == 0

    def test_wide_path_many_rebals(self):
        # Zigzag price: 100 → 200 → 100 → 200 ... 12 swings × $100
        prices = [(i * 3600000, 100.0 if i % 2 == 0 else 200.0) for i in range(24)]
        n = ce.expected_rebalances_24h(prices, 120.0, 180.0)
        # path ≈ 23 * 100 = 2300; width = 60 → n ≈ 38
        assert n > 20, f"expected many rebals, got {n}"

    def test_degenerate_width(self):
        # Zero-width range returns high sentinel, preventing div-by-zero explosion
        prices = [(0, 100.0), (1, 200.0)]
        assert ce.expected_rebalances_24h(prices, 150.0, 150.0) >= 100


class TestCalcMyL:
    def test_basic_weth_usdc(self):
        """Module-level calc_my_L assumes WETH/USDC (18d / 6d, stable at token1)."""
        L = ce.calc_my_L(400.0, 2400.0, 2300.0, 2500.0)
        assert L > 0
        assert 1e13 < L < 1e16, f"L={L:.2e} out of expected order"


if __name__ == "__main__":
    failed = 0
    classes = [
        TestTickPriceRoundtrip, TestAvgLInRange, TestTimeInRange,
        TestRecentApy, TestExpectedRebalances, TestCalcMyL,
    ]
    for cls in classes:
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
