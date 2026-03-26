"""跨交易所资金费率套利引擎。

策略：在费率低的交易所做多 perp，在费率高的交易所做空 perp。
两腿等量 delta-neutral，赚取 funding spread。
数据源：VarFunding API（预计算的套利机会）。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from shared.bn_client import BinanceClient
from shared.emit import emit, emit_error
from shared.hl_client import HLClient
from shared.state import load_state, save_state

from .varfunding_scanner import VarFundingScanner

STATE_NAME = "cross_funding"


class CrossFundingEngine:
    def __init__(
        self,
        hl_client: HLClient,
        bn_client: BinanceClient,
        scanner: VarFundingScanner,
        cfg: dict,
    ) -> None:
        self.hl = hl_client
        self.bn = bn_client
        self.scanner = scanner
        self.cfg = cfg
        self.hl_budget: float = cfg["hl_budget_usd"]
        self.bn_budget: float = cfg["bn_budget_usd"]
        self.leverage: int = cfg["leverage"]
        self.min_apr: float = cfg["min_apr_pct"]
        self.stability_snapshots: int = cfg.get("stability_snapshots", 3)
        self.close_spread_threshold: float = cfg.get("close_spread_threshold", 0.00005)
        self.switch_threshold_apr: float = cfg.get("switch_threshold_apr", 5.0)
        self.max_price_basis_pct: float = cfg.get("max_price_basis_pct", 0.5)
        self.round_trip_cost_pct: float = cfg.get("round_trip_cost_pct", 0.12)

    # ---- 状态管理 ----

    def _load(self) -> dict:
        return load_state(STATE_NAME)

    def _save(self, state: dict) -> None:
        state["last_tick"] = datetime.now(timezone.utc).isoformat()
        save_state(STATE_NAME, state)

    # ---- 客户端路由 ----

    def _get_client(self, exchange: str) -> HLClient | BinanceClient:
        if exchange == "hyperliquid":
            return self.hl
        return self.bn

    def _get_mid_price(self, exchange: str, coin: str) -> float:
        return self._get_client(exchange).get_mid_price(coin)

    def _get_funding_rate(self, exchange: str, coin: str) -> float:
        return self._get_client(exchange).get_funding_rate(coin)

    # ---- 扫描 ----

    def scan_opportunities(self) -> list[dict]:
        """通过 VarFunding API 扫描套利机会。"""
        return self.scanner.fetch_opportunities()

    def record_snapshot(self, opportunities: list[dict]) -> None:
        """记录一次快照到 state，用于稳定性验证。"""
        state = self._load()
        snapshots = state.get("rate_snapshots", [])

        now = datetime.now(timezone.utc).isoformat()
        for opp in opportunities[:5]:  # 只记录 top 5
            snapshots.append(
                {
                    "ts": now,
                    "coin": opp["coin"],
                    "spread": opp["spread"],
                    "estimated_apr": opp["estimated_apr"],
                    "long_exchange": opp["long_exchange"],
                    "short_exchange": opp["short_exchange"],
                }
            )

        # 只保留最近 20 条
        state["rate_snapshots"] = snapshots[-20:]
        self._save(state)

    def get_stable_opportunity(self) -> dict | None:
        """从累积快照中找到稳定的最佳机会。

        需要同一 coin 出现 stability_snapshots 次以上且 spread 稳定。
        """
        state = self._load()
        snapshots = state.get("rate_snapshots", [])
        if not snapshots:
            return None

        # 按 coin 分组
        by_coin: dict[str, list[dict]] = {}
        for s in snapshots:
            coin = s["coin"]
            by_coin.setdefault(coin, []).append(s)

        best: dict | None = None
        best_apr = 0.0

        for coin, coin_snaps in by_coin.items():
            stability = self.scanner.check_stability(coin_snaps)
            if not stability["stable"]:
                continue

            # 取最新快照的 apr
            latest = coin_snaps[-1]
            apr = latest.get("estimated_apr", 0.0)
            if apr > best_apr:
                best_apr = apr
                best = latest

        return best

    # ---- 深度验证 ----

    def verify_opportunity(self, coin: str, direction: dict) -> dict:
        """独立验证两所实际费率 + 价格差 + 往返成本。

        Args:
            direction: {"long_exchange": str, "short_exchange": str}

        Returns:
            {
                "valid": bool,
                "hl_rate": float, "bn_rate": float,
                "actual_spread": float,
                "hl_price": float, "bn_price": float,
                "price_basis_pct": float,
                "round_trip_cost_pct": float,
                "net_apr_after_costs": float,
                "reject_reason": str | None,
            }
        """
        long_ex = direction["long_exchange"]
        short_ex = direction["short_exchange"]

        hl_rate = self.hl.get_funding_rate(coin)
        bn_rate = self.bn.get_funding_rate(coin)
        hl_price = self.hl.get_mid_price(coin)
        bn_price = self.bn.get_mid_price(coin)

        # 实际 spread = short_rate - long_rate（做空方收费率减去做多方付费率）
        rate_map = {"hyperliquid": hl_rate, "binance": bn_rate}
        actual_spread = rate_map[short_ex] - rate_map[long_ex]

        # 价格差异
        avg_price = (hl_price + bn_price) / 2
        price_basis_pct = abs(hl_price - bn_price) / avg_price * 100 if avg_price else 0

        # 年化 APR（扣除往返成本）
        # spread 是 8h 费率差，年化 = spread × 3 × 365 × 100
        gross_annual = actual_spread * 3 * 365 * 100
        net_apr = gross_annual - self.round_trip_cost_pct

        reject_reason = None
        if actual_spread <= 0:
            reject_reason = f"spread non-positive: {actual_spread:.6f}"
        elif price_basis_pct > self.max_price_basis_pct:
            reject_reason = f"price basis too large: {price_basis_pct:.2f}%"
        elif net_apr < self.min_apr:
            reject_reason = f"net APR too low: {net_apr:.1f}%"

        result = {
            "valid": reject_reason is None,
            "hl_rate": hl_rate,
            "bn_rate": bn_rate,
            "actual_spread": actual_spread,
            "hl_price": hl_price,
            "bn_price": bn_price,
            "price_basis_pct": round(price_basis_pct, 4),
            "round_trip_cost_pct": self.round_trip_cost_pct,
            "net_apr_after_costs": round(net_apr, 2),
            "reject_reason": reject_reason,
        }

        emit("verify_opportunity", {"coin": coin, **result})
        return result

    # ---- 开仓 ----

    def _calculate_size(self, budget_per_exchange: float, price: float) -> float:
        """计算单腿 size。

        effective_margin = budget × 0.95（预留手续费）
        size = effective_margin × leverage / price
        """
        if price <= 0:
            return 0.0
        effective = budget_per_exchange * 0.95
        return effective * self.leverage / price

    def open_position(self, coin: str, direction: dict) -> bool:
        """原子开仓：先 HL 腿（限制更严），后 Binance 腿。失败回滚。

        HL 分级保证金对小币要求高，若首次尝试失败会自动减半 size 重试（最多 3 次）。

        Args:
            direction: {"long_exchange": str, "short_exchange": str}
        """
        state = self._load()
        if state.get("current_coin"):
            emit(
                "warn",
                {"msg": f"already has position in {state['current_coin']}, skip open"},
            )
            return False

        long_ex = direction["long_exchange"]
        short_ex = direction["short_exchange"]

        # 确定哪边是 HL，哪边是 Binance（HL 先下单，限制更严）
        hl_is_long = long_ex == "hyperliquid"
        hl_side = "long" if hl_is_long else "short"
        bn_side = "short" if hl_is_long else "long"

        price = self.hl.get_mid_price(coin)
        if price <= 0:
            emit_error("price", RuntimeError(f"invalid price for {coin}: {price}"))
            return False

        # 预算 = min(两所)，保守系数 0.5（HL 分级保证金对小币要求更高）
        budget = min(self.hl_budget, self.bn_budget)
        conservative = budget * 0.5
        raw_size = self._calculate_size(conservative, price)

        # 两所分别 round，取较小值
        hl_rounded = self.hl.round_size(coin, raw_size)
        bn_rounded = self.bn.round_size(coin, raw_size)
        size = min(hl_rounded, bn_rounded)
        if size <= 0:
            emit_error("calc_size", RuntimeError(f"size=0 for {coin}"))
            return False

        emit(
            "open_sizing",
            {
                "coin": coin,
                "budget": budget,
                "conservative_budget": conservative,
                "price": price,
                "raw_size": raw_size,
                "final_size": size,
                "notional": round(size * price, 2),
            },
        )

        # 1) 两所设杠杆
        try:
            self.hl.set_leverage(coin, self.leverage, cross=True)
        except Exception as e:
            emit_error("set_leverage_hl", e)
        try:
            self.bn.set_leverage(coin, self.leverage)
        except Exception as e:
            emit_error("set_leverage_bn", e)

        # 2) 先下 HL 单（限制更严，失败代价低）
        #    若 margin 不足，自动减半 size 重试（最多 3 轮）
        #    slippage 收紧到 0.1%（funding arb 对成本敏感，5% 太宽）
        arb_slippage = 0.001
        hl_is_buy = hl_is_long
        hl_result = None
        for attempt in range(4):
            try:
                hl_result = self.hl.market_order(
                    coin, is_buy=hl_is_buy, size=size, slippage=arb_slippage
                )
                if not _is_order_error(hl_result):
                    break  # 成功
                err_msg = str(hl_result)
                if "Insufficient margin" in err_msg and attempt < 3:
                    size = self.hl.round_size(coin, size * 0.5)
                    size = min(size, self.bn.round_size(coin, size))
                    if size <= 0 or size * price < 10:
                        emit(
                            "size_retry",
                            {
                                "coin": coin,
                                "abort": True,
                                "reason": "size too small after halving",
                            },
                        )
                        break
                    emit(
                        "size_retry",
                        {
                            "coin": coin,
                            "attempt": attempt + 1,
                            "new_size": size,
                            "new_notional": round(size * price, 2),
                        },
                    )
                    continue
                break  # 其他错误，不重试
            except Exception as e:
                emit_error(f"hl_{hl_side}_order", e)
                return False

        if hl_result is None or _is_order_error(hl_result):
            emit_error(
                f"hl_{hl_side}_order",
                RuntimeError(f"HL order failed after retries: {hl_result}"),
            )
            return False
        time.sleep(1)

        # 3) 再下 Binance 单（使用 HL 实际成交的 size）
        bn_is_buy = not hl_is_long
        bn_size = self.bn.round_size(coin, size)
        try:
            bn_result = self.bn.market_order(
                coin, is_buy=bn_is_buy, size=bn_size, slippage=arb_slippage
            )
            if _is_order_error(bn_result):
                emit_error(
                    f"bn_{bn_side}_order",
                    RuntimeError(f"BN order failed: {bn_result}"),
                )
                emit("rollback", {"reason": "BN leg failed", "coin": coin})
                try:
                    self.hl.close_position(coin)
                except Exception as re:
                    emit_error("rollback_hl", re, notify=True)
                return False
        except Exception as e:
            emit_error(f"bn_{bn_side}_order", e)
            emit("rollback", {"reason": "BN leg failed", "coin": coin})
            try:
                self.hl.close_position(coin)
            except Exception as re:
                emit_error("rollback_hl", re, notify=True)
            return False
        time.sleep(1)

        # 保存状态
        hl_rate = self.hl.get_funding_rate(coin)
        bn_rate = self.bn.get_funding_rate(coin)

        # 记录开仓时两所实际余额，用于后续计算真实收益率
        entry_hl_balance = self.hl.get_usdc_balance()
        entry_bn_balance = self.bn.get_usdt_balance()

        state = {
            "current_coin": coin,
            "direction": direction,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_spread": abs(hl_rate - bn_rate),
            "entry_hl_rate": hl_rate,
            "entry_bn_rate": bn_rate,
            "size": size,
            "entry_price": price,
            "budget_hl": self.hl_budget,
            "budget_bn": self.bn_budget,
            "entry_hl_balance": entry_hl_balance,
            "entry_bn_balance": entry_bn_balance,
            "entry_total_balance": round(entry_hl_balance + entry_bn_balance, 2),
            "total_funding_earned": 0.0,
            "rate_snapshots": [],
        }
        self._save(state)

        emit(
            "position_opened",
            {
                "coin": coin,
                "size": size,
                "long_exchange": long_ex,
                "short_exchange": short_ex,
                "leverage": self.leverage,
                "entry_price": price,
                "hl_rate": hl_rate,
                "bn_rate": bn_rate,
            },
            notify=True,
            tier="trade_alert",
        )
        return True

    # ---- 平仓 ----

    def close_position(self, coin: str) -> bool:
        """平掉两腿。先平 short，后平 long。"""
        state = self._load()
        direction = state.get("direction", {})
        long_ex = direction.get("long_exchange", "hyperliquid")
        short_ex = direction.get("short_exchange", "binance")
        long_client = self._get_client(long_ex)
        short_client = self._get_client(short_ex)

        # 1) 平 short 腿
        try:
            short_client.close_position(coin)
        except Exception as e:
            emit_error("close_short", e)
        time.sleep(1)

        # 2) 平 long 腿
        try:
            long_client.close_position(coin)
        except Exception as e:
            emit_error("close_long", e)
        time.sleep(1)

        funding_earned = state.get("total_funding_earned", 0.0)
        emit(
            "position_closed",
            {
                "coin": coin,
                "long_exchange": long_ex,
                "short_exchange": short_ex,
                "funding_earned": funding_earned,
            },
            notify=True,
            tier="trade_alert",
        )

        self._save({})
        return True

    # ---- 健康检查 ----

    def check_health(self) -> dict:
        """检查两腿仓位一致性 + spread 是否仍然有利。"""
        state = self._load()
        coin = state.get("current_coin")
        if not coin:
            return {"healthy": True, "has_position": False}

        direction = state.get("direction", {})
        long_ex = direction.get("long_exchange", "hyperliquid")
        short_ex = direction.get("short_exchange", "binance")
        long_client = self._get_client(long_ex)
        short_client = self._get_client(short_ex)

        long_pos = long_client.get_position(coin)
        short_pos = short_client.get_position(coin)

        long_size = abs(long_pos["size"]) if long_pos else 0.0
        short_size = abs(short_pos["size"]) if short_pos else 0.0

        # Delta 检查
        avg_size = (long_size + short_size) / 2 if (long_size + short_size) > 0 else 1
        delta_pct = abs(long_size - short_size) / avg_size * 100

        # 当前 spread
        hl_rate = self.hl.get_funding_rate(coin)
        bn_rate = self.bn.get_funding_rate(coin)
        rate_map = {"hyperliquid": hl_rate, "binance": bn_rate}
        current_spread = rate_map[short_ex] - rate_map[long_ex]
        current_apr = current_spread * 3 * 365 * 100

        # 判断是否有利
        spread_favorable = current_spread > self.close_spread_threshold

        # 判断健康
        has_both_legs = long_size > 0 and short_size > 0
        healthy = has_both_legs and spread_favorable and delta_pct < 20

        result = {
            "healthy": healthy,
            "has_position": True,
            "coin": coin,
            "long_exchange": long_ex,
            "short_exchange": short_ex,
            "long_size": long_size,
            "short_size": short_size,
            "delta_pct": round(delta_pct, 2),
            "current_spread": current_spread,
            "current_apr": round(current_apr, 2),
            "spread_favorable": spread_favorable,
            "has_both_legs": has_both_legs,
            "hl_rate": hl_rate,
            "bn_rate": bn_rate,
        }

        if not healthy:
            emit("health_warning", result, notify=True, tier="risk_alert")

        return result

    # ---- 切仓 ----

    def check_and_switch(self) -> bool:
        """spread 不利时平仓或切换到更好的机会。"""
        state = self._load()
        coin = state.get("current_coin")
        if not coin:
            return False

        health = self.check_health()
        if health["healthy"]:
            return False

        # spread 不利 → 平仓
        if not health["spread_favorable"]:
            emit(
                "switch_close",
                {
                    "coin": coin,
                    "reason": "spread unfavorable",
                    "current_spread": health["current_spread"],
                    "current_apr": health["current_apr"],
                },
                notify=True,
                tier="risk_alert",
            )
            self.close_position(coin)
            return True

        # 缺腿 → 平仓
        if not health["has_both_legs"]:
            emit(
                "switch_close",
                {
                    "coin": coin,
                    "reason": "missing leg",
                    "long_size": health["long_size"],
                    "short_size": health["short_size"],
                },
                notify=True,
                tier="risk_alert",
            )
            self.close_position(coin)
            return True

        return False

    # ---- 报告 ----

    def get_status(self) -> dict:
        """获取当前状态摘要。"""
        state = self._load()
        coin = state.get("current_coin")
        if not coin:
            return {
                "has_position": False,
                "hl_balance": self.hl.get_usdc_balance(),
                "bn_balance": self.bn.get_usdt_balance(),
            }

        health = self.check_health()
        return {
            "has_position": True,
            "coin": coin,
            "direction": state.get("direction"),
            "entry_time": state.get("entry_time"),
            "size": state.get("size"),
            "entry_price": state.get("entry_price"),
            "entry_spread": state.get("entry_spread"),
            "current_spread": health.get("current_spread"),
            "current_apr": health.get("current_apr"),
            "hl_rate": health.get("hl_rate"),
            "bn_rate": health.get("bn_rate"),
            "long_size": health.get("long_size"),
            "short_size": health.get("short_size"),
            "delta_pct": health.get("delta_pct"),
            "healthy": health.get("healthy"),
            "total_funding_earned": state.get("total_funding_earned", 0.0),
            "hl_balance": self.hl.get_usdc_balance(),
            "bn_balance": self.bn.get_usdt_balance(),
        }

    def get_report(self) -> dict:
        """生成完整报告，含基于实际余额的收益率。"""
        status = self.get_status()
        state = self._load()

        # 初始余额（开仓时记录）
        entry_total = state.get("entry_total_balance", 0.0)
        entry_hl = state.get("entry_hl_balance", 0.0)
        entry_bn = state.get("entry_bn_balance", 0.0)

        # 当前余额
        current_hl = status.get("hl_balance", 0.0)
        current_bn = status.get("bn_balance", 0.0)
        current_total = round(current_hl + current_bn, 2)

        # 实际 PnL = 当前总余额 - 初始总余额
        pnl = round(current_total - entry_total, 2) if entry_total else 0.0
        roi_pct = round(pnl / entry_total * 100, 4) if entry_total else 0.0

        # 年化：PnL / 持仓时间 × 365天
        annualized_roi_pct = 0.0
        entry_time = state.get("entry_time")
        if entry_time and entry_total:
            from datetime import datetime, timezone

            try:
                entry_dt = datetime.fromisoformat(entry_time)
                hours_held = (
                    datetime.now(timezone.utc) - entry_dt
                ).total_seconds() / 3600
                if hours_held > 0:
                    annualized_roi_pct = round(roi_pct / hours_held * 24 * 365, 2)
            except (ValueError, TypeError):
                pass

        return {
            **status,
            "entry_hl_balance": entry_hl,
            "entry_bn_balance": entry_bn,
            "entry_total_balance": entry_total,
            "current_total_balance": current_total,
            "pnl": pnl,
            "roi_pct": roi_pct,
            "annualized_roi_pct": annualized_roi_pct,
        }


def _is_order_error(result: dict) -> bool:
    """检查下单结果是否为错误。兼容 HL 和 Binance 返回格式。"""
    # HL 格式: {"status": "error"} 或 response.data.statuses[0].error
    if result.get("status") in ("error", "err"):
        return True
    try:
        s = result["response"]["data"]["statuses"][0]
        return "error" in s
    except (KeyError, IndexError, TypeError):
        pass
    # Binance 格式: {"code": -xxxx, "msg": "..."}
    if "code" in result and int(result.get("code", 0)) < 0:
        return True
    return False
