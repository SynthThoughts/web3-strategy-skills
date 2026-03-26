"""配置加载：.env + config.json"""

from __future__ import annotations

import json
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SCRIPT_DIR / "config.json"


def load_config() -> dict:
    """加载 config.json，返回完整配置字典。"""
    with open(CONFIG_PATH) as f:
        return json.load(f)


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key, "")
    if not v:
        return default
    return v.lower() in ("true", "1", "yes")


# 常用环境变量快捷访问
def hl_private_key() -> str:
    k = env("HL_PRIVATE_KEY")
    if not k:
        raise RuntimeError("HL_PRIVATE_KEY not set")
    return k


def hl_testnet() -> bool:
    return env_bool("HL_TESTNET", default=False)


def hl_vault_address() -> str:
    return env("HL_VAULT_ADDRESS")


def state_dir() -> Path:
    d = env("STATE_DIR")
    return Path(d) if d else SCRIPT_DIR


def discord_webhook_url() -> str:
    return env("DISCORD_WEBHOOK_URL")


def binance_api_key() -> str:
    k = env("BINANCE_API_KEY")
    if not k:
        raise RuntimeError("BINANCE_API_KEY not set")
    return k


def binance_secret_key() -> str:
    k = env("BINANCE_SECRET_KEY")
    if not k:
        raise RuntimeError("BINANCE_SECRET_KEY not set")
    return k


def bn_testnet() -> bool:
    return env_bool("BINANCE_TESTNET", default=False)
