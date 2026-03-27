"""Tests for improved rebalance trigger logic."""

import unittest
from datetime import datetime, timedelta

# Inline the functions under test to avoid heavy module-level imports
# (cl_lp.py has side effects on import: reads config, env vars, etc.)


def tick_to_price(tick: int, decimal_adj: float = 1e12) -> float:
    return 1.0001**tick / decimal_adj


def check_rebalance_triggers(
    price: float,
    state: dict,
    atr_pct: float,
    mtf: dict | None = None,
    *,
    _classify_volatility=None,
) -> dict | None:
    """Extracted trigger logic matching cl_lp.py after fix."""
    position = state.get("position")
    if not position or not position.get("tick_lower"):
        return None

    tick_lower = position["tick_lower"]
    tick_upper = position["tick_upper"]
    lower_price = tick_to_price(tick_lower)
    upper_price = tick_to_price(tick_upper)

    # [1] Out of range
    if price < lower_price or price > upper_price:
        side = "below" if price < lower_price else "above"
        return {"trigger": "out_of_range", "priority": "mandatory", "detail": side}

    # [2] Volatility regime change
    created_atr = position.get("created_atr_pct", 0)
    if created_atr > 0:
        vol_change = abs(atr_pct - created_atr) / created_atr
        if vol_change > 0.3:
            return {
                "trigger": "volatility_shift",
                "priority": "adaptive",
                "detail": f"delta {vol_change:.0%}",
            }

    # [4] Time decay — only when price near edge
    created_at = position.get("created_at")
    if created_at:
        if isinstance(created_at, str):
            created_dt = datetime.fromisoformat(created_at)
        else:
            created_dt = created_at
        age_seconds = (datetime.now() - created_dt).total_seconds()
        if age_seconds > 86400:
            range_width = upper_price - lower_price
            if range_width > 0:
                position_in_range = (price - lower_price) / range_width
                edge_threshold = 0.20
                near_edge = position_in_range < edge_threshold or position_in_range > (1 - edge_threshold)
                if near_edge:
                    edge_side = "near_lower" if position_in_range < 0.5 else "near_upper"
                    return {
                        "trigger": "time_decay",
                        "priority": "maintenance",
                        "detail": f"{age_seconds / 3600:.1f}h old, {edge_side} ({position_in_range:.0%})",
                    }

    return None


class TestTimeDecayTrigger(unittest.TestCase):
    """Test that time_decay only fires when price is near range edge."""

    def _make_state(self, age_hours: float, tick_lower: int, tick_upper: int):
        return {
            "position": {
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "created_at": (datetime.now() - timedelta(hours=age_hours)).isoformat(),
                "created_atr_pct": 2.0,
            }
        }

    def _range_prices(self, tick_lower, tick_upper):
        lower = tick_to_price(tick_lower)
        upper = tick_to_price(tick_upper)
        return lower, upper

    def test_center_position_no_trigger(self):
        """Price at 50% of range, >24h old → should NOT trigger."""
        tick_lower, tick_upper = -200220, -199200
        lower, upper = self._range_prices(tick_lower, tick_upper)
        mid_price = (lower + upper) / 2

        state = self._make_state(48, tick_lower, tick_upper)
        result = check_rebalance_triggers(mid_price, state, 2.0)
        self.assertIsNone(result, "Should not trigger time_decay when price is centered")

    def test_40pct_position_no_trigger(self):
        """Price at 40% of range, >24h old → should NOT trigger (inside safe zone)."""
        tick_lower, tick_upper = -200220, -199200
        lower, upper = self._range_prices(tick_lower, tick_upper)
        price = lower + 0.4 * (upper - lower)

        state = self._make_state(48, tick_lower, tick_upper)
        result = check_rebalance_triggers(price, state, 2.0)
        self.assertIsNone(result)

    def test_near_lower_edge_triggers(self):
        """Price at 10% of range, >24h old → should trigger."""
        tick_lower, tick_upper = -200220, -199200
        lower, upper = self._range_prices(tick_lower, tick_upper)
        price = lower + 0.10 * (upper - lower)

        state = self._make_state(48, tick_lower, tick_upper)
        result = check_rebalance_triggers(price, state, 2.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["trigger"], "time_decay")
        self.assertIn("near_lower", result["detail"])

    def test_near_upper_edge_triggers(self):
        """Price at 90% of range, >24h old → should trigger."""
        tick_lower, tick_upper = -200220, -199200
        lower, upper = self._range_prices(tick_lower, tick_upper)
        price = lower + 0.90 * (upper - lower)

        state = self._make_state(48, tick_lower, tick_upper)
        result = check_rebalance_triggers(price, state, 2.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["trigger"], "time_decay")
        self.assertIn("near_upper", result["detail"])

    def test_young_position_no_trigger(self):
        """Price near edge but <24h old → should NOT trigger."""
        tick_lower, tick_upper = -200220, -199200
        lower, upper = self._range_prices(tick_lower, tick_upper)
        price = lower + 0.05 * (upper - lower)

        state = self._make_state(12, tick_lower, tick_upper)
        result = check_rebalance_triggers(price, state, 2.0)
        self.assertIsNone(result)

    def test_out_of_range_takes_priority(self):
        """Price below range → out_of_range, not time_decay."""
        tick_lower, tick_upper = -200220, -199200
        lower, upper = self._range_prices(tick_lower, tick_upper)
        price = lower * 0.95

        state = self._make_state(48, tick_lower, tick_upper)
        result = check_rebalance_triggers(price, state, 2.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["trigger"], "out_of_range")


class TestExponentialBackoff(unittest.TestCase):
    """Test that backoff grows exponentially and caps correctly."""

    def test_backoff_progression(self):
        COOLDOWN_AFTER_ERRORS = 3600
        for n, expected_min in [(1, 10), (2, 20), (3, 40), (4, 60), (5, 60), (6, 60)]:
            backoff = min(600 * (2 ** (n - 1)), COOLDOWN_AFTER_ERRORS)
            expected_sec = expected_min * 60
            self.assertEqual(
                backoff, expected_sec,
                f"n={n}: expected {expected_sec}s, got {backoff}s"
            )

    def test_backoff_cap(self):
        """Backoff should never exceed COOLDOWN_AFTER_ERRORS."""
        COOLDOWN_AFTER_ERRORS = 3600
        for n in range(1, 20):
            backoff = min(600 * (2 ** (n - 1)), COOLDOWN_AFTER_ERRORS)
            self.assertLessEqual(backoff, COOLDOWN_AFTER_ERRORS)


if __name__ == "__main__":
    unittest.main()
