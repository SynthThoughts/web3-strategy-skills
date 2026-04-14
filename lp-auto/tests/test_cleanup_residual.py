"""Unit test for cleanup_residual_positions — verify it respects state.edges
and doesn't kill range-order NFTs tracked by edge_manager.

Regression test for the 2026-04-15 incident where a freshly minted range-order
NFT (4969650) was redeemed by cleanup 4 seconds after mint because the NFT
wasn't in state.position.token_id.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "references"))


class TestCleanupRespectsEdges:
    def _setup(self, state_data):
        """Create a temp INSTANCE_DIR + minimal config/state, return cl_lp module."""
        tmp = tempfile.mkdtemp(prefix="lp_cleanup_test_")
        Path(tmp, "state.json").write_text(json.dumps(state_data))
        # Minimal config needed for cl_lp import (merges with defaults)
        Path(tmp, "config.json").write_text(json.dumps({
            "chain": "base",
            "pool_config": {
                "investment_id": "326890603",
                "chain": "base", "chain_index": "8453",
                "token0_symbol": "WETH",
                "token0_address": "0x4200000000000000000000000000000000000006",
                "token0_decimals": 18,
                "token1_symbol": "USDC",
                "token1_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "token1_decimals": 6,
                "fee_tier": 0.003, "tick_spacing": 60,
            },
        }))
        os.environ["LP_AUTO_INSTANCE_DIR"] = tmp
        for m in list(sys.modules):
            if m == "cl_lp":
                del sys.modules[m]
        import cl_lp
        return cl_lp, tmp

    def test_keeps_main_and_edges(self):
        state_data = {
            "position": {"token_id": "MAIN_ID"},
            "edges": [
                {"token_id": "EDGE_A", "side": "buy_weth"},
                {"token_id": "EDGE_B", "side": "sell_weth"},
            ],
        }
        cl_lp, tmp = self._setup(state_data)
        # Mock on-chain calls
        positions = [
            {"tokenId": "MAIN_ID", "value": 100.0},
            {"tokenId": "EDGE_A",  "value":   5.0},
            {"tokenId": "EDGE_B",  "value":   5.0},
            {"tokenId": "ORPHAN",  "value":   2.0},
            {"tokenId": "DUST",    "value":   0.001},
        ]
        with patch.object(cl_lp, "_query_all_positions", return_value=positions), \
             patch.object(cl_lp, "defi_redeem", return_value=True) as redeem:
            count = cl_lp.cleanup_residual_positions("MAIN_ID")
        # Should only redeem ORPHAN (MAIN/EDGE_A/EDGE_B kept; DUST skipped)
        assert count == 1, f"expected 1 orphan cleaned, got {count}"
        redeemed_ids = [call.args[0] for call in redeem.call_args_list]
        assert redeemed_ids == ["ORPHAN"], redeemed_ids

    def test_no_edges_behaves_like_legacy(self):
        """When state.edges is missing or empty, behave identically to v1."""
        state_data = {"position": {"token_id": "MAIN_ID"}}
        cl_lp, tmp = self._setup(state_data)
        positions = [
            {"tokenId": "MAIN_ID", "value": 100.0},
            {"tokenId": "ORPHAN1", "value":   5.0},
            {"tokenId": "ORPHAN2", "value":   3.0},
        ]
        with patch.object(cl_lp, "_query_all_positions", return_value=positions), \
             patch.object(cl_lp, "defi_redeem", return_value=True) as redeem:
            count = cl_lp.cleanup_residual_positions("MAIN_ID")
        assert count == 2, f"expected 2 cleaned (no edges), got {count}"
        assert {call.args[0] for call in redeem.call_args_list} == {"ORPHAN1", "ORPHAN2"}

    def test_extra_keep_ids_overrides(self):
        state_data = {
            "position": {"token_id": "MAIN_ID"},
            "edges": [{"token_id": "EDGE_A", "side": "buy_weth"}],
        }
        cl_lp, tmp = self._setup(state_data)
        positions = [
            {"tokenId": "MAIN_ID", "value": 100.0},
            {"tokenId": "EDGE_A",  "value":   5.0},
            {"tokenId": "EXTRA_X", "value":   5.0},
        ]
        # Caller explicitly passes its own keep set — should NOT merge state.edges
        with patch.object(cl_lp, "_query_all_positions", return_value=positions), \
             patch.object(cl_lp, "defi_redeem", return_value=True) as redeem:
            count = cl_lp.cleanup_residual_positions(
                "MAIN_ID", extra_keep_ids={"EXTRA_X"}
            )
        # EDGE_A NOT in extra_keep_ids → redeemed; EXTRA_X protected
        assert count == 1
        assert [c.args[0] for c in redeem.call_args_list] == ["EDGE_A"]

    def test_string_coercion(self):
        """token_id can be int or str; cleanup must compare as strings."""
        state_data = {
            "position": {"token_id": 12345},   # int
            "edges": [{"token_id": "67890"}],  # str
        }
        cl_lp, tmp = self._setup(state_data)
        positions = [
            {"tokenId": "12345", "value": 100.0},
            {"tokenId": "67890", "value":   5.0},
            {"tokenId": "99999", "value":   3.0},
        ]
        with patch.object(cl_lp, "_query_all_positions", return_value=positions), \
             patch.object(cl_lp, "defi_redeem", return_value=True) as redeem:
            count = cl_lp.cleanup_residual_positions(12345)  # passed as int
        # Only 99999 should be cleaned
        assert count == 1
        assert [c.args[0] for c in redeem.call_args_list] == ["99999"]


if __name__ == "__main__":
    failed = 0
    for cls in [TestCleanupRespectsEdges]:
        inst = cls()
        for name in dir(inst):
            if not name.startswith("test_"):
                continue
            try:
                getattr(inst, name)()
                print(f"  ✓ {cls.__name__}.{name}")
            except Exception as e:
                import traceback
                print(f"  ✗ {cls.__name__}.{name}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n{'FAILED' if failed else 'PASSED'} — {failed} failures")
    sys.exit(0 if failed == 0 else 1)
