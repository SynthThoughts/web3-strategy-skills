"""熔断器：连续错误超限后进入冷却。"""

from __future__ import annotations

import time

from .config import load_config
from .emit import emit


class CircuitBreaker:
    def __init__(self) -> None:
        cfg = load_config()["shared"]
        self.max_errors: int = cfg["max_consecutive_errors"]
        self.cooldown: int = cfg["cooldown_after_errors"]
        self.consecutive_errors = 0
        self.cooldown_until = 0.0

    def is_open(self) -> bool:
        if time.time() < self.cooldown_until:
            return True
        if self.cooldown_until > 0 and time.time() >= self.cooldown_until:
            # 冷却结束，重置
            self.consecutive_errors = 0
            self.cooldown_until = 0.0
        return False

    def record_success(self) -> None:
        self.consecutive_errors = 0
        self.cooldown_until = 0.0

    def record_error(self, context: str = "") -> bool:
        """记录错误，返回 True 表示触发熔断。"""
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.max_errors:
            self.cooldown_until = time.time() + self.cooldown
            emit(
                "circuit_breaker",
                {
                    "status": "open",
                    "errors": self.consecutive_errors,
                    "cooldown_s": self.cooldown,
                    "context": context,
                },
                notify=True,
            )
            return True
        return False
