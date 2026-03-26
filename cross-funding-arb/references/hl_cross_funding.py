"""跨交易所资金费率套利入口。

Usage:
    python -m funding.hl_cross_funding tick
    python -m funding.hl_cross_funding report
    python -m funding.hl_cross_funding status
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from shared.bn_client import BinanceClient
from shared.circuit_breaker import CircuitBreaker
from shared.config import (
    binance_api_key,
    binance_secret_key,
    bn_testnet,
    hl_private_key,
    hl_testnet,
    hl_vault_address,
    load_config,
)
from shared.emit import emit, emit_error
from shared.hl_client import HLClient
from shared.state import acquire_lock, release_lock

from .cross_funding_engine import CrossFundingEngine
from .varfunding_scanner import VarFundingScanner

LOCK_NAME = "cross_funding"
PULSE_INTERVAL_SECONDS = 3600  # 1h

_cb = CircuitBreaker()


def _build_engine() -> CrossFundingEngine:
    cfg = load_config()
    cross_cfg = cfg["cross_funding"]

    hl_client = HLClient(
        hl_private_key(), testnet=hl_testnet(), vault_address=hl_vault_address()
    )
    bn_client = BinanceClient(
        binance_api_key(), binance_secret_key(), testnet=bn_testnet()
    )
    scanner = VarFundingScanner(
        min_apr=cross_cfg["min_apr_pct"],
        min_confidence=cross_cfg.get("min_confidence", "medium"),
        stability_threshold=cross_cfg.get("stability_max_std_ratio", 0.3),
    )
    return CrossFundingEngine(hl_client, bn_client, scanner, cross_cfg)


def _should_pulse(state: dict) -> bool:
    """Check if enough time has passed since last hourly pulse."""
    last_pulse = state.get("last_pulse_ts")
    if not last_pulse:
        return True
    try:
        last_dt = datetime.fromisoformat(last_pulse)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed >= PULSE_INTERVAL_SECONDS
    except (ValueError, TypeError):
        return True


def tick() -> None:
    """主循环 tick：扫描 → 稳定性验证 → 开仓/维护 + 定时 pulse。"""
    if not acquire_lock(LOCK_NAME):
        emit("skip", {"reason": "another instance running"})
        return

    try:
        if _cb.is_open():
            emit("skip", {"reason": "circuit breaker open"})
            return

        engine = _build_engine()
        state = engine._load()
        coin = state.get("current_coin")

        if not coin:
            # 无仓位：扫描 → 记录快照 → 检查稳定性 → 验证 → 开仓
            opportunities = engine.scan_opportunities()
            if not opportunities:
                emit("tick", {"action": "idle", "reason": "no opportunities"})
                return

            engine.record_snapshot(opportunities)

            stable_opp = engine.get_stable_opportunity()
            if not stable_opp:
                emit(
                    "tick",
                    {
                        "action": "accumulating",
                        "reason": "waiting for rate stability",
                        "top_coin": opportunities[0]["coin"],
                        "top_apr": opportunities[0]["estimated_apr"],
                    },
                )
                return

            # 深度验证
            direction = {
                "long_exchange": stable_opp["long_exchange"],
                "short_exchange": stable_opp["short_exchange"],
            }
            verification = engine.verify_opportunity(stable_opp["coin"], direction)
            if not verification["valid"]:
                emit(
                    "tick",
                    {
                        "action": "rejected",
                        "coin": stable_opp["coin"],
                        "reason": verification["reject_reason"],
                    },
                )
                return

            emit(
                "tick",
                {
                    "action": "opening",
                    "coin": stable_opp["coin"],
                    "apr": verification["net_apr_after_costs"],
                    "direction": direction,
                },
            )
            success = engine.open_position(stable_opp["coin"], direction)
            if not success:
                _cb.record_error("open_position")
                return
        else:
            # 有仓位：健康检查 + 切仓判断
            switched = engine.check_and_switch()
            if not switched:
                health = engine.check_health()
                report_data = engine.get_report()
                snapshot = {**report_data, **health}

                # 每次 tick 都缓存快照，status 查询可直接读取
                state = engine._load()
                state["cached_snapshot"] = snapshot
                state["cached_snapshot_ts"] = datetime.now(timezone.utc).isoformat()

                # 每小时推送一次 pulse 卡片
                if _should_pulse(state):
                    emit("tick", snapshot, tier="hourly_pulse")
                    state["last_pulse_ts"] = datetime.now(timezone.utc).isoformat()
                else:
                    emit(
                        "tick",
                        {
                            "action": "hold",
                            "coin": coin,
                            "current_apr": health.get("current_apr"),
                            "delta_pct": health.get("delta_pct"),
                            "healthy": health.get("healthy"),
                        },
                    )

                engine._save(state)

        _cb.record_success()

    except Exception as e:
        emit_error("tick", e, notify=_cb.record_error("tick"))
    finally:
        release_lock()


def report() -> None:
    """生成并输出报告（含日报通知卡片）。"""
    try:
        engine = _build_engine()
        data = engine.get_report()
        emit("report", data, notify=True, tier="daily_report")
    except Exception as e:
        emit_error("report", e, notify=True)


def status() -> None:
    """输出当前状态卡片。优先读缓存（tick 每 5 分钟更新），无缓存时实时计算。"""
    try:
        from shared.state import load_state

        state = load_state("cross_funding")
        cached = state.get("cached_snapshot")
        if cached:
            cached["_cached_ts"] = state.get("cached_snapshot_ts", "")
            emit("status", cached, tier="hourly_pulse")
        else:
            engine = _build_engine()
            data = engine.get_report()
            emit("status", data, tier="hourly_pulse")
    except Exception as e:
        emit_error("status", e)


def main() -> None:
    from dotenv import load_dotenv

    script_dir = Path(__file__).resolve().parent.parent
    load_dotenv(script_dir / ".env")

    parser = argparse.ArgumentParser(
        description="Cross-exchange funding rate arbitrage"
    )
    parser.add_argument(
        "command", choices=["tick", "report", "status"], help="subcommand to run"
    )
    args = parser.parse_args()

    commands = {
        "tick": tick,
        "report": report,
        "status": status,
    }
    commands[args.command]()


if __name__ == "__main__":
    main()
