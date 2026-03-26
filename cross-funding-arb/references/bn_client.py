"""Binance USDS-M Futures 最小封装。"""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlencode

import requests


class BinanceClient:
    """Binance USDS-M Futures REST API 客户端。"""

    MAINNET_URL = "https://fapi.binance.com"
    TESTNET_URL = "https://demo-fapi.binance.com"
    RECV_WINDOW = 10_000
    TIME_SYNC_TTL = 30

    def __init__(self, api_key: str, secret_key: str, testnet: bool = False) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = self.TESTNET_URL if testnet else self.MAINNET_URL

        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-MBX-APIKEY": self.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )

        # Exchange info cache (1h TTL)
        self._exchange_info_cache: dict | None = None
        self._exchange_info_ts: float = 0.0
        self._cache_ttl: float = 3600.0

        # Server time offset
        self._ts_offset_ms: int = 0
        self._ts_synced_at: float = 0.0

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _get_timestamp(self) -> int:
        """返回经过服务器校准的毫秒时间戳。"""
        return int(time.time() * 1000) + self._ts_offset_ms

    def _sync_server_time(self, force: bool = False) -> None:
        """与 Binance 服务器同步时间，使用往返中点降低延迟影响。"""
        now = time.time()
        if (
            not force
            and self._ts_synced_at
            and (now - self._ts_synced_at) < self.TIME_SYNC_TTL
        ):
            return

        url = f"{self.base_url}/fapi/v1/time"
        t0 = int(time.time() * 1000)
        resp = self.session.get(url, timeout=5)
        t1 = int(time.time() * 1000)
        resp.raise_for_status()

        server_time = int(resp.json()["serverTime"])
        midpoint = (t0 + t1) // 2
        self._ts_offset_ms = server_time - midpoint
        self._ts_synced_at = time.time()

    def _sign(self, params: dict) -> str:
        """HMAC-SHA256 签名。"""
        query_string = urlencode(params)
        return hmac.new(
            self.secret_key.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        signed: bool = False,
    ) -> dict:
        """发送 HTTP 请求，signed 请求自动附加时间戳和签名。

        对 -1021 (Timestamp outside recvWindow) 自动重试一次。
        """
        url = f"{self.base_url}{endpoint}"
        base_params = dict(params or {})
        max_attempts = 2 if signed else 1

        for attempt in range(max_attempts):
            req_params = dict(base_params)

            if signed:
                self._sync_server_time(force=(attempt > 0))
                req_params["timestamp"] = self._get_timestamp()
                req_params["recvWindow"] = self.RECV_WINDOW
                req_params["signature"] = self._sign(req_params)

            if method == "GET":
                resp = self.session.get(url, params=req_params, timeout=10)
            else:
                resp = self.session.post(url, data=req_params, timeout=10)

            if resp.status_code != 200:
                try:
                    err = resp.json()
                except ValueError:
                    err = {}
                code = err.get("code", resp.status_code)
                msg = err.get("msg", resp.text)

                # Timestamp drift → force resync and retry once
                if (
                    signed
                    and attempt == 0
                    and (str(code) == "-1021" or "recvWindow" in str(msg))
                ):
                    self._ts_synced_at = 0.0
                    continue

                raise RuntimeError(f"Binance API Error {code}: {msg}")

            return resp.json()

        raise RuntimeError(f"Binance request failed after retries: {method} {endpoint}")

    def _to_symbol(self, coin: str) -> str:
        """ETH -> ETHUSDT"""
        return f"{coin}USDT"

    def _get_exchange_info(self) -> dict:
        """获取 exchangeInfo，带 1h 缓存。"""
        now = time.time()
        if (
            self._exchange_info_cache
            and (now - self._exchange_info_ts) < self._cache_ttl
        ):
            return self._exchange_info_cache
        self._exchange_info_cache = self._request("GET", "/fapi/v1/exchangeInfo")
        self._exchange_info_ts = now
        return self._exchange_info_cache

    def _get_precision(self, symbol: str) -> dict:
        """返回 {tick_size, step_size, min_qty}。"""
        info = self._get_exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                result: dict = {
                    "tick_size": "0.01",
                    "step_size": "0.001",
                    "min_qty": "0.001",
                }
                for f in s.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        result["tick_size"] = f["tickSize"]
                    elif f["filterType"] == "LOT_SIZE":
                        result["step_size"] = f["stepSize"]
                        result["min_qty"] = f["minQty"]
                return result
        raise RuntimeError(f"Symbol {symbol} not found in exchange info")

    # ------------------------------------------------------------------ #
    #  Market Data                                                         #
    # ------------------------------------------------------------------ #

    def get_mid_price(self, coin: str) -> float:
        """获取标记价格。"""
        symbol = self._to_symbol(coin)
        data = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["markPrice"])

    def get_funding_rate(self, coin: str) -> float:
        """获取最新资金费率。"""
        symbol = self._to_symbol(coin)
        data = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["lastFundingRate"])

    def get_all_funding_rates(self) -> dict[str, float]:
        """获取所有 USDT 永续合约的资金费率，键为 coin (去掉 USDT 后缀)。"""
        data = self._request("GET", "/fapi/v1/premiumIndex")
        rates: dict[str, float] = {}
        for item in data:
            symbol: str = item["symbol"]
            if symbol.endswith("USDT"):
                coin = symbol[: -len("USDT")]
                rates[coin] = float(item["lastFundingRate"])
        return rates

    # ------------------------------------------------------------------ #
    #  Account                                                             #
    # ------------------------------------------------------------------ #

    def get_usdt_balance(self) -> float:
        """获取 USDT 余额。"""
        data = self._request("GET", "/fapi/v2/balance", signed=True)
        for asset in data:
            if asset["asset"] == "USDT":
                return float(asset["balance"])
        return 0.0

    def get_position(self, coin: str) -> dict | None:
        """获取持仓信息，无仓位返回 None。"""
        symbol = self._to_symbol(coin)
        data = self._request(
            "GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True
        )
        for pos in data:
            if pos["symbol"] == symbol:
                size = float(pos["positionAmt"])
                if size == 0:
                    return None
                return {
                    "coin": coin,
                    "size": size,
                    "entry_px": float(pos["entryPrice"]),
                    "unrealized_pnl": float(pos["unRealizedProfit"]),
                }
        return None

    # ------------------------------------------------------------------ #
    #  Trading                                                             #
    # ------------------------------------------------------------------ #

    def set_leverage(self, coin: str, leverage: int) -> None:
        """设置杠杆倍数。"""
        symbol = self._to_symbol(coin)
        self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    def market_order(
        self, coin: str, is_buy: bool, size: float, *, slippage: float = 0.0
    ) -> dict:
        """市价下单。slippage>0 时改用 LIMIT IOC 控制最大滑点。"""
        symbol = self._to_symbol(coin)
        size = self.round_size(coin, size)
        if slippage > 0:
            mid = self.get_mid_price(coin)
            px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
            px = self._round_price(coin, px)
            params = {
                "symbol": symbol,
                "side": "BUY" if is_buy else "SELL",
                "type": "LIMIT",
                "quantity": size,
                "price": px,
                "timeInForce": "IOC",
            }
        else:
            params = {
                "symbol": symbol,
                "side": "BUY" if is_buy else "SELL",
                "type": "MARKET",
                "quantity": size,
            }
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def close_position(self, coin: str) -> dict | None:
        """市价平仓，无仓位返回 None。"""
        pos = self.get_position(coin)
        if not pos or pos["size"] == 0:
            return None
        is_buy = pos["size"] < 0  # 空仓→买入平仓
        size = abs(pos["size"])
        return self.market_order(coin, is_buy, size)

    # ------------------------------------------------------------------ #
    #  Utils                                                               #
    # ------------------------------------------------------------------ #

    def round_size(self, coin: str, size: float) -> float:
        """根据 exchange info 的 step_size 向下取整。"""
        symbol = self._to_symbol(coin)
        precision = self._get_precision(symbol)
        step = Decimal(precision["step_size"])
        d_size = Decimal(str(size))
        rounded = (d_size / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(rounded)

    def _round_price(self, coin: str, price: float) -> float:
        """根据 exchange info 的 tick_size 取整。"""
        symbol = self._to_symbol(coin)
        precision = self._get_precision(symbol)
        tick = Decimal(precision["tick_size"])
        d_price = Decimal(str(price))
        rounded = (d_price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return float(rounded)
