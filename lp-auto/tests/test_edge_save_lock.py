"""Regression test for 2026-04-15 race: edge_manager.save_edges must not
lose writes when a concurrent cl_lp tick rewrites state.

Scenario reproduced:
  1. edge_manager.run mints edge, save_edges([edge]) writes state.edges=[A]
  2. ~30s later, cron-triggered tick runs, load_state returns old snapshot
     (without edge A), writes back, overwriting edge_manager's write
  3. Next tick's cleanup sees A as orphan, redeems it
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "references"))


def _new_instance():
    tmp = tempfile.mkdtemp(prefix="lp_edge_lock_")
    Path(tmp, "state.json").write_text(json.dumps({"position": {"token_id": "MAIN"}, "edges": []}))
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
        if m in ("cl_lp", "edge_manager", "range_order_direct"):
            del sys.modules[m]
    return tmp


class TestSaveEdgesLock:
    # NOTE on threading vs process:
    #   Linux flock is per-file-description (not per-process). Two threads in
    #   the same process opening distinct fds on the same file can BOTH hold
    #   LOCK_EX without blocking each other. Real protection is against
    #   separate processes (cron tick subprocess + edge_manager subprocess),
    #   which IS enforced. We therefore test the cross-process case with
    #   actual subprocess.

    def test_external_save_blocks_on_held_lock_via_subprocess(self):
        import subprocess, threading, textwrap, time
        tmp = _new_instance()
        # Parent: spawn a child process that holds the lock for ~3s
        holder_script = textwrap.dedent(f"""
            import os, fcntl, time, sys
            os.environ['LP_AUTO_INSTANCE_DIR'] = '{tmp}'
            sys.path.insert(0, '{str(Path(__file__).parent.parent / "references")}')
            import cl_lp
            cl_lp._acquire_lock()
            time.sleep(3)
            cl_lp._release_lock()
        """)
        holder = subprocess.Popen(["python3", "-c", holder_script])
        time.sleep(0.5)  # let holder acquire

        # In parent, call save_edges — should block for ~2.5s until holder releases
        import cl_lp, edge_manager
        from edge_manager import Edge
        edge = Edge(token_id="X1", side="buy_weth",
                   tick_lower=-199000, tick_upper=-198900,
                   amount_raw=1000000, token="0x833589fc",
                   created_at="t", created_tick=-198950, liquidity=1)
        t0 = time.time()
        edge_manager.save_edges([edge])
        elapsed = time.time() - t0
        holder.wait(timeout=5)
        assert elapsed > 1.5, f"save_edges returned in {elapsed:.2f}s — didn't block"
        state = cl_lp.load_state()
        assert any(e["token_id"] == "X1" for e in state["edges"])

    def test_internal_save_reuses_lock(self):
        """When called inside a tick (lock already held by this process via
        cl_lp._acquire_lock), save_edges should NOT try to re-acquire."""
        _new_instance()
        import cl_lp
        import edge_manager
        from edge_manager import Edge

        assert cl_lp._acquire_lock() is True
        try:
            edge = Edge(token_id="E2", side="sell_weth",
                       tick_lower=-197000, tick_upper=-196900,
                       amount_raw=500000, token="0x42",
                       created_at="t", created_tick=-197000, liquidity=1)
            edge_manager.save_edges([edge])
            # Verify state updated
            state = cl_lp.load_state()
            assert state["edges"][0]["token_id"] == "E2"
        finally:
            cl_lp._release_lock()


if __name__ == "__main__":
    failed = 0
    for cls in [TestSaveEdgesLock]:
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
