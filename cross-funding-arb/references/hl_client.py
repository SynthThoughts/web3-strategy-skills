"""Hyperliquid SDK 封装：Info（行情）+ Exchange（交易）。"""

from __future__ import annotations

import math
import time

import eth_account
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .emit import emit


class HLClient:
    """统一封装 Hyperliquid 读 + 写 API。"""

    def __init__(
        self, private_key: str, testnet: bool = False, vault_address: str = ""
    ) -> None:
        self.account = eth_account.Account.from_key(private_key)
        self.wallet_address = self.account.address
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.base_url = base_url
        self.testnet = testnet

        # testnet spot_meta 可能为空导致 IndexError，安全初始化
        info_kwargs: dict = {"skip_ws": True}
        try:
            self.info = Info(base_url, **info_kwargs)
        except (IndexError, KeyError):
            # spot_meta 解析失败（testnet 常见），跳过现货元数据
            self.info = Info(
                base_url, skip_ws=True, spot_meta={"tokens": [], "universe": []}
            )

        exchange_kwargs: dict = {}
        if vault_address:
            exchange_kwargs["account_address"] = vault_address

        try:
            self.exchange = Exchange(self.account, base_url, **exchange_kwargs)
        except (IndexError, KeyError):
            self.exchange = Exchange(
                self.account,
                base_url,
                spot_meta={"tokens": [], "universe": []},
                **exchange_kwargs,
            )

        # Agent wallet: 订单路由到 master，查询也要用 master 地址
        if vault_address:
            self.address = vault_address
            self.balance_address = vault_address
        else:
            master = self._resolve_master_address()
            if master:
                self.address = master
                self.balance_address = master
            else:
                self.address = self.wallet_address
                self.balance_address = self.wallet_address

        # 缓存元数据
        self._meta: dict[str, object] | None = None
        self._spot_meta: dict | None = None
        self._sz_decimals: dict[str, int] = {}

    def _resolve_master_address(self) -> str | None:
        """检测当前 key 是否为 agent wallet，返回 master account 地址。

        Agent wallet 下的订单归属 master account，余额在 agent wallet 自身。
        通过 ledger 中的 deposit 来源推断 master 地址。
        返回 None 表示不是 agent wallet（key 本身就是 master）。
        """
        try:
            # 检查 wallet 的 ledger，找 deposit 来源（即 master）
            resp = requests.post(
                f"{self.base_url}/info",
                json={
                    "type": "userNonFundingLedgerUpdates",
                    "user": self.wallet_address,
                    "startTime": 0,
                },
                timeout=5,
            )
            updates = resp.json()
            for u in updates:
                delta = u.get("delta", {})
                if delta.get("type") == "send":
                    source = delta.get("user", "")
                    if source and source.lower() != self.wallet_address.lower():
                        emit(
                            "agent_wallet_detected",
                            {
                                "agent": self.wallet_address,
                                "master": source,
                            },
                        )
                        return source
        except Exception:
            pass
        return None

    # ---- 元数据 ----

    def _ensure_meta(self) -> None:
        if self._meta is None:
            self._meta = self.info.meta()
            for asset in self._meta["universe"]:
                self._sz_decimals[asset["name"]] = asset["szDecimals"]

    def _ensure_spot_meta(self) -> None:
        if self._spot_meta is None:
            self._spot_meta = self.info.spot_meta()

    def sz_decimals(self, coin: str) -> int:
        self._ensure_meta()
        return self._sz_decimals.get(coin, 2)

    # ---- 行情 ----

    def get_mid_price(self, coin: str) -> float:
        mids = self.info.all_mids()
        return float(mids[coin])

    def get_candles(
        self, coin: str, interval: str = "1h", count: int = 24
    ) -> list[dict]:
        """获取 K 线数据，返回 [{t, o, h, l, c, v}, ...]。"""
        now_ms = int(time.time() * 1000)
        # 估算需要的时间跨度
        interval_ms = _interval_to_ms(interval)
        start_ms = now_ms - interval_ms * count
        raw = self.info.candles_snapshot(coin, interval, start_ms, now_ms)
        result = []
        for c in raw:
            result.append(
                {
                    "t": c["t"],
                    "o": float(c["o"]),
                    "h": float(c["h"]),
                    "l": float(c["l"]),
                    "c": float(c["c"]),
                    "v": float(c["v"]),
                }
            )
        return result[-count:]

    def get_funding_rate(self, coin: str) -> float:
        """获取单个币种的当前资金费率（每 8h）。"""
        ctx = self.info.meta_and_asset_ctxs()
        for asset_meta, asset_ctx in zip(ctx[0]["universe"], ctx[1]):
            if asset_meta["name"] == coin:
                return float(asset_ctx["funding"])
        return 0.0

    def get_all_funding_rates(self) -> dict[str, float]:
        """获取所有永续合约的当前资金费率。"""
        ctx = self.info.meta_and_asset_ctxs()
        rates = {}
        for asset_meta, asset_ctx in zip(ctx[0]["universe"], ctx[1]):
            rates[asset_meta["name"]] = float(asset_ctx["funding"])
        return rates

    # ---- 账户 ----

    def get_usdc_balance(self) -> float:
        """获取可用余额（perp accountValue + spot USDC）。

        HL 的 cross-margin 模式下 spot USDC 也可用作 perp 保证金。
        """
        state = self.info.user_state(self.balance_address)
        perp_value = float(state["marginSummary"]["accountValue"])
        # 加上 spot USDC（cross-margin 模式下可用作保证金）
        try:
            spot_state = self.info.spot_user_state(self.balance_address)
            for bal in spot_state.get("balances", []):
                if bal["coin"] == "USDC":
                    perp_value += float(bal["total"]) - float(bal["hold"])
                    break
        except Exception:
            pass
        return perp_value

    def get_withdrawable(self) -> float:
        state = self.info.user_state(self.balance_address)
        return float(state["withdrawable"])

    def get_position(self, coin: str) -> dict | None:
        """返回持仓信息 dict 或 None。"""
        state = self.info.user_state(self.address)
        for pos in state.get("assetPositions", []):
            item = pos["position"]
            if item["coin"] == coin:
                return {
                    "coin": item["coin"],
                    "size": float(item["szi"]),
                    "entry_px": float(item["entryPx"]) if item.get("entryPx") else 0.0,
                    "unrealized_pnl": float(item["unrealizedPnl"]),
                    "leverage_type": item["leverage"]["type"],
                    "leverage_value": int(item["leverage"]["value"]),
                    "liquidation_px": float(item["liquidationPx"])
                    if item.get("liquidationPx")
                    else 0.0,
                }
        return None

    def get_all_positions(self) -> list[dict]:
        state = self.info.user_state(self.address)
        positions = []
        for pos in state.get("assetPositions", []):
            item = pos["position"]
            sz = float(item["szi"])
            if sz != 0:
                positions.append(
                    {
                        "coin": item["coin"],
                        "size": sz,
                        "entry_px": float(item["entryPx"])
                        if item.get("entryPx")
                        else 0.0,
                        "unrealized_pnl": float(item["unrealizedPnl"]),
                    }
                )
        return positions

    def get_spot_balance(self, coin: str) -> float:
        """获取现货可用余额（total - hold）。"""
        spot_coin = _perp_to_spot_token(coin)
        balances = self.info.spot_user_state(self.balance_address)
        for bal in balances.get("balances", []):
            if bal["coin"] == spot_coin:
                return float(bal["total"]) - float(bal["hold"])
        return 0.0

    def get_spot_usdc(self) -> float:
        """获取现货账户 USDC 余额。"""
        balances = self.info.spot_user_state(self.balance_address)
        for bal in balances.get("balances", []):
            if bal["coin"] == "USDC":
                return float(bal["total"]) - float(bal["hold"])
        return 0.0

    def get_coins_with_spot_and_perp(self) -> set[str]:
        """返回同时有现货和永续市场的币种名（永续名称）。"""
        self._ensure_meta()
        self._ensure_spot_meta()
        perp_names = {a["name"] for a in self._meta["universe"]}  # type: ignore[index]
        spot_tokens = set()
        for token in self._spot_meta.get("tokens", []):  # type: ignore[union-attr]
            name = token["name"]
            if name == "USDC":
                continue
            # UBTC → BTC, UETH → ETH, others keep name
            perp_name = (
                name[1:] if name.startswith("U") and name[1:] in perp_names else name
            )
            if perp_name in perp_names:
                spot_tokens.add(perp_name)
        return spot_tokens

    def transfer_to_spot(self, usd_amount: float) -> dict:
        """从永续账户转 USDC 到现货账户。"""
        return self.exchange.usd_class_transfer(usd_amount, to_perp=False)

    def transfer_to_perp(self, usd_amount: float) -> dict:
        """从现货账户转 USDC 到永续账户。"""
        return self.exchange.usd_class_transfer(usd_amount, to_perp=True)

    def get_open_orders(self, coin: str | None = None) -> list[dict]:
        """获取当前挂单。coin=None 返回所有。"""
        orders = self.info.open_orders(self.address)
        result = []
        for o in orders:
            if coin and o["coin"] != coin:
                continue
            result.append(
                {
                    "oid": o["oid"],
                    "coin": o["coin"],
                    "side": "buy" if o["side"] == "B" else "sell",
                    "size": float(o["sz"]),
                    "price": float(o["limitPx"]),
                    "order_type": o.get("orderType", "limit"),
                }
            )
        return result

    # ---- 交易 ----

    def set_leverage(self, coin: str, leverage: int, cross: bool = False) -> None:
        self.exchange.update_leverage(leverage, coin, is_cross=cross)

    def limit_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        *,
        reduce_only: bool = False,
    ) -> dict:
        size = self.round_size(coin, size)
        price = self._round_price(price)
        if size <= 0:
            return {"status": "error", "msg": "size too small"}
        result = self.exchange.order(
            coin,
            is_buy,
            size,
            price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=reduce_only,
        )
        self._log_order("limit", coin, is_buy, size, price, result)
        return result

    def market_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        *,
        slippage: float = 0.05,
    ) -> dict:
        size = self.round_size(coin, size)
        if size <= 0:
            return {"status": "error", "msg": "size too small"}
        mid = self.get_mid_price(coin)
        # 市价单用限价模拟，加滑点
        px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
        px = self._round_price(px)
        result = self.exchange.order(
            coin,
            is_buy,
            size,
            px,
            {"limit": {"tif": "Ioc"}},  # Immediate-or-Cancel
        )
        self._log_order("market", coin, is_buy, size, px, result)
        return result

    def place_tp(self, coin: str, is_buy: bool, size: float, trigger_px: float) -> dict:
        """止盈单（触发后市价成交）。"""
        size = self.round_size(coin, size)
        trigger_px = self._round_price(trigger_px)
        result = self.exchange.order(
            coin,
            is_buy,
            size,
            trigger_px,
            {"trigger": {"isMarket": True, "triggerPx": str(trigger_px), "tpsl": "tp"}},
            reduce_only=True,
        )
        self._log_order("tp", coin, is_buy, size, trigger_px, result)
        return result

    def place_sl(self, coin: str, is_buy: bool, size: float, trigger_px: float) -> dict:
        """止损单（触发后市价成交）。"""
        size = self.round_size(coin, size)
        trigger_px = self._round_price(trigger_px)
        result = self.exchange.order(
            coin,
            is_buy,
            size,
            trigger_px,
            {"trigger": {"isMarket": True, "triggerPx": str(trigger_px), "tpsl": "sl"}},
            reduce_only=True,
        )
        self._log_order("sl", coin, is_buy, size, trigger_px, result)
        return result

    def cancel_all(self, coin: str) -> None:
        orders = self.info.open_orders(self.address)
        for o in orders:
            if o["coin"] == coin:
                try:
                    self.exchange.cancel(coin, o["oid"])
                except Exception:
                    pass  # 已取消或已成交

    def spot_market_buy(
        self, coin: str, size: float, *, slippage: float = 0.05
    ) -> dict:
        """现货市价买入。coin 用永续名称（如 'ETH'），内部转换为现货对。"""
        spot_pair = _perp_to_spot_pair(coin)
        size = self.round_size(coin, size)
        if size <= 0:
            return {"status": "error", "msg": "size too small"}
        mid = self.get_mid_price(spot_pair)
        px = self._round_price(mid * (1 + slippage))
        result = self.exchange.order(
            spot_pair, True, size, px, {"limit": {"tif": "Ioc"}}
        )
        self._log_order("spot_buy", coin, True, size, px, result)
        return result

    def spot_market_sell(
        self, coin: str, size: float, *, slippage: float = 0.05
    ) -> dict:
        """现货市价卖出。"""
        spot_pair = _perp_to_spot_pair(coin)
        size = self.round_size(coin, size)
        if size <= 0:
            return {"status": "error", "msg": "size too small"}
        mid = self.get_mid_price(spot_pair)
        px = self._round_price(mid * (1 - slippage))
        result = self.exchange.order(
            spot_pair, False, size, px, {"limit": {"tif": "Ioc"}}
        )
        self._log_order("spot_sell", coin, False, size, px, result)
        return result

    def close_position(self, coin: str) -> dict | None:
        """市价平仓。"""
        pos = self.get_position(coin)
        if not pos or pos["size"] == 0:
            return None
        is_buy = pos["size"] < 0  # 空仓则买入平仓
        size = abs(pos["size"])
        return self.market_order(coin, is_buy, size)

    # ---- 工具 ----

    def round_size(self, coin: str, size: float) -> float:
        decimals = self.sz_decimals(coin)
        factor = 10**decimals
        return math.floor(size * factor) / factor

    def _round_price(self, price: float) -> float:
        """价格取 5 位有效数字。"""
        if price <= 0:
            return 0.0
        magnitude = 10 ** math.floor(math.log10(price))
        return round(price / magnitude, 4) * magnitude

    def _log_order(
        self,
        kind: str,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        result: dict,
    ) -> None:
        status = "ok"
        error = ""
        oid = None
        if result.get("status") == "err":
            status = "error"
            error = result.get("response", "")
        elif "response" in result and "data" in result["response"]:
            data = result["response"]["data"]
            if "statuses" in data and data["statuses"]:
                s = data["statuses"][0]
                if "error" in s:
                    status = "error"
                    error = s["error"]
                elif "resting" in s:
                    oid = s["resting"]["oid"]
                elif "filled" in s:
                    oid = s["filled"]["oid"]
        emit(
            "order",
            {
                "kind": kind,
                "coin": coin,
                "side": "buy" if is_buy else "sell",
                "size": size,
                "price": price,
                "status": status,
                "oid": oid,
                "error": error,
            },
        )


# ---- 现货名称映射 ----

# HL 现货特殊命名: BTC→UBTC, ETH→UETH, 其他币同名
_SPOT_TOKEN_MAP = {"BTC": "UBTC", "ETH": "UETH"}
_SPOT_TOKEN_REVERSE = {v: k for k, v in _SPOT_TOKEN_MAP.items()}


def _perp_to_spot_token(coin: str) -> str:
    """永续名 → 现货 token 名: 'ETH' → 'UETH', 'SOL' → 'SOL'。"""
    return _SPOT_TOKEN_MAP.get(coin, coin)


def _perp_to_spot_pair(coin: str) -> str:
    """永续名 → 现货交易对: 'ETH' → 'UETH/USDC'。"""
    return f"{_perp_to_spot_token(coin)}/USDC"


def _spot_token_to_perp(token: str) -> str:
    """现货 token 名 → 永续名: 'UETH' → 'ETH'。"""
    return _SPOT_TOKEN_REVERSE.get(token, token)


def _interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    val = int(interval[:-1])
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return val * multipliers.get(unit, 3_600_000)
