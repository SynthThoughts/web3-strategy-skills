"""VarFunding API 封装 + 机会过滤 + 稳定性分析。"""

from __future__ import annotations

import statistics

import requests

from shared.emit import emit

CONFIDENCE_LEVELS = {"high": 3, "medium": 2, "low": 1}

TARGET_EXCHANGES = {"hyperliquid", "binance"}


class VarFundingScanner:
    API_URL = "https://varfunding.xyz/api/funding"
    TIMEOUT = 15

    def __init__(
        self,
        min_apr: float = 10.0,
        min_confidence: str = "medium",
        stability_threshold: float = 0.3,
    ):
        self.min_apr = min_apr
        self.min_confidence_level = CONFIDENCE_LEVELS.get(min_confidence, 2)
        self.stability_threshold = stability_threshold

    def fetch_opportunities(
        self,
        exchanges: tuple[str, str] = ("hyperliquid", "binance"),
    ) -> list[dict]:
        """Fetch and filter arbitrage opportunities from VarFunding API.

        Returns list of dicts sorted by estimated_apr descending:
        [{
            "coin": "ETH",
            "long_exchange": "hyperliquid",
            "short_exchange": "binance",
            "spread": 0.0006,
            "estimated_apr": 66.5,
            "confidence": "medium",
            "hl_rate": 0.0000125,
            "bn_rate": 0.0006194,
        }]
        """
        resp = requests.get(
            self.API_URL,
            params={"exchanges": ",".join(exchanges)},
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        exchange_set = set(exchanges)
        results: list[dict] = []

        for market in data.get("markets", []):
            arb = market.get("arbitrageOpportunity")
            if not arb:
                continue

            long_ex = arb.get("longExchange", "")
            short_ex = arb.get("shortExchange", "")

            # Only keep pairs where BOTH sides are in our target exchanges
            if long_ex not in exchange_set or short_ex not in exchange_set:
                continue

            confidence = arb.get("confidence", "low")
            if CONFIDENCE_LEVELS.get(confidence, 0) < self.min_confidence_level:
                continue

            estimated_apr = arb.get("estimatedApr", 0.0)
            if estimated_apr < self.min_apr:
                continue

            # Extract per-exchange rates from comparisons + variational
            rate_map: dict[str, float] = {}
            var = market.get("variational")
            if var and var.get("exchange") in exchange_set:
                rate_map[var["exchange"]] = var.get("rate", 0.0)
            for comp in market.get("comparisons", []):
                if comp.get("exchange") in exchange_set:
                    rate_map[comp["exchange"]] = comp.get("rate", 0.0)

            results.append(
                {
                    "coin": market.get("baseAsset", ""),
                    "long_exchange": long_ex,
                    "short_exchange": short_ex,
                    "spread": arb.get("spread", 0.0),
                    "estimated_apr": estimated_apr,
                    "confidence": confidence,
                    "hl_rate": rate_map.get("hyperliquid", 0.0),
                    "bn_rate": rate_map.get("binance", 0.0),
                }
            )

        results.sort(key=lambda x: x["estimated_apr"], reverse=True)

        emit(
            "varfunding_scan",
            {
                "count": len(results),
                "top_5": [
                    {"coin": r["coin"], "apr": round(r["estimated_apr"], 1)}
                    for r in results[:5]
                ],
            },
        )

        return results

    def check_stability(self, snapshots: list[dict]) -> dict:
        """Analyze multiple snapshots for rate stability.

        Args:
            snapshots: list of dicts, each with "coin", "spread", "ts"

        Returns:
            {
                "stable": bool,     # True if enough snapshots and spread is stable
                "count": int,       # number of snapshots
                "avg_spread": float,
                "std_spread": float,
                "std_ratio": float, # std / avg (lower = more stable)
            }
        """
        count = len(snapshots)
        if count < 3:
            return {
                "stable": False,
                "count": count,
                "avg_spread": 0.0,
                "std_spread": 0.0,
                "std_ratio": 0.0,
            }

        spreads = [s["spread"] for s in snapshots]
        avg_spread = statistics.mean(spreads)
        std_spread = statistics.stdev(spreads)

        if avg_spread == 0:
            std_ratio = float("inf")
        else:
            std_ratio = std_spread / abs(avg_spread)

        return {
            "stable": std_ratio < self.stability_threshold,
            "count": count,
            "avg_spread": avg_spread,
            "std_spread": std_spread,
            "std_ratio": std_ratio,
        }
