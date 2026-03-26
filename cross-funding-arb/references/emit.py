"""结构化 JSON 事件输出 + 通知推送。

策略输出结构化 JSON 到 stdout，同时按 tier 构建 Discord embed + Telegram
markdown 并推送。凭证解析优先级：环境变量 > ZeroClaw config.toml。
未配置凭证时静默跳过，不影响策略运行。
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Notification credentials ────────────────────────────────────────────────


def _parse_toml_section(text: str, section: str) -> dict[str, str]:
    """从 TOML 文本中提取指定 section 的 key=value 对。简单解析，无需依赖。"""
    result: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped.rstrip("]").lstrip("[").strip() == section
            continue
        if in_section and "=" in stripped and not stripped.startswith("#"):
            k, v = stripped.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _read_zeroclaw_config() -> dict[str, str]:
    """读取 ZeroClaw config.toml，按实例优先级查找。

    查找顺序: zeroclaw-strategy > zeroclaw > zeroclaw-data > zeroclaw-ops
    """
    for instance in ["zeroclaw-strategy", "zeroclaw", "zeroclaw-data", "zeroclaw-ops"]:
        cfg_path = Path.home() / f".{instance}" / "config.toml"
        if cfg_path.exists():
            try:
                return {"_text": cfg_path.read_text(), "_instance": instance}
            except Exception:
                pass
    return {}


_ZC_CONFIG = _read_zeroclaw_config()


def _get_discord_token() -> str:
    """Discord bot token: 环境变量 > ZeroClaw channels_config.discord.bot_token"""
    env_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if env_token:
        return env_token
    text = _ZC_CONFIG.get("_text", "")
    if text:
        section = _parse_toml_section(text, "channels_config.discord")
        return section.get("bot_token", "")
    return ""


def _get_discord_channel_id() -> str:
    """Discord channel ID: 仅从环境变量读取（每个策略自行配置目标频道）。"""
    return os.environ.get("DISCORD_CHANNEL_ID", "")


def _get_telegram_config() -> tuple[str, str]:
    """Telegram 凭证: 环境变量 > ZeroClaw channels_config.telegram。

    bot_token 从 ZeroClaw config fallback，chat_id 从环境变量读取。
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token:
        text = _ZC_CONFIG.get("_text", "")
        if text:
            section = _parse_toml_section(text, "channels_config.telegram")
            token = section.get("bot_token", "")
    return token, chat_id


DISCORD_CHANNEL_ID = _get_discord_channel_id()
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = _get_telegram_config()

# ── Notification card builder ────────────────────────────────────────────────

STRATEGY_LABEL = "Cross-Funding"


def _build_notification(tier: str, data: dict) -> dict | None:
    """Build dual-format notification (discord embed + text markdown).

    Returns {"tier": str, "discord": {...}, "text": str} or None if silent.
    """

    # ── Trade Alert (open / close) ───────────────────────────────────────
    if tier == "trade_alert":
        event = data.get("type", "")
        coin = data.get("coin", "?")
        long_ex = data.get("long_exchange", "?")
        short_ex = data.get("short_exchange", "?")

        if event == "position_opened":
            size = data.get("size", 0)
            price = data.get("entry_price", 0)
            notional = round(size * price, 2)
            leverage = data.get("leverage", 1)
            hl_rate = data.get("hl_rate", 0)
            bn_rate = data.get("bn_rate", 0)
            spread = abs(bn_rate - hl_rate)
            apr = spread * 3 * 365 * 100

            fields = [
                {"name": "Long", "value": long_ex.title(), "inline": True},
                {"name": "Short", "value": short_ex.title(), "inline": True},
                {"name": "杠杆", "value": f"{leverage}x", "inline": True},
                {"name": "Size", "value": f"{size:g} {coin}", "inline": True},
                {"name": "名义价值", "value": f"${notional:,.2f}", "inline": True},
                {"name": "Spread", "value": f"{spread:.4%}", "inline": True},
                {
                    "name": "预估 APR",
                    "value": f"{apr:.1f}%",
                    "inline": True,
                },
            ]

            text_lines = [
                f"🔄 **开仓 · {coin} · {STRATEGY_LABEL}**",
                f"📍 Long `{long_ex}` / Short `{short_ex}` | `{leverage}x`",
                f"📦 Size: `{size:g} {coin}` (`${notional:,.2f}`)",
                f"📈 Spread: `{spread:.4%}` → `{apr:.1f}%` APR",
            ]

            return {
                "tier": "trade_alert",
                "discord": {
                    "title": f"🔄 开仓 · {coin} · {STRATEGY_LABEL}",
                    "color": 0x00CC66,
                    "fields": fields,
                },
                "text": "\n".join(text_lines),
            }

        if event == "position_closed":
            funding = data.get("funding_earned", 0)

            text_lines = [
                f"📤 **平仓 · {coin} · {STRATEGY_LABEL}**",
                f"💵 Funding Earned: `${funding:,.2f}`",
            ]

            return {
                "tier": "trade_alert",
                "discord": {
                    "title": f"📤 平仓 · {coin} · {STRATEGY_LABEL}",
                    "color": 0xFF6600,
                    "fields": [
                        {
                            "name": "Funding Earned",
                            "value": f"${funding:,.2f}",
                            "inline": True,
                        },
                    ],
                },
                "text": "\n".join(text_lines),
            }

        return None

    # ── Risk Alert ───────────────────────────────────────────────────────
    if tier == "risk_alert":
        coin = data.get("coin", "?")
        reason = data.get("reason", data.get("context", "unknown"))
        current_apr = data.get("current_apr", 0)
        delta_pct = data.get("delta_pct", 0)

        fields = [
            {"name": "原因", "value": str(reason), "inline": False},
            {"name": "当前 APR", "value": f"{current_apr:.1f}%", "inline": True},
            {"name": "Delta 偏差", "value": f"{delta_pct:.1f}%", "inline": True},
        ]

        text_lines = [
            f"🛑 **风险告警 · {coin} · {STRATEGY_LABEL}**",
            f"⚠️ 原因: `{reason}`",
            f"📊 APR: `{current_apr:.1f}%` | Delta: `{delta_pct:.1f}%`",
        ]

        return {
            "tier": "risk_alert",
            "discord": {
                "title": f"🛑 风险告警 · {coin} · {STRATEGY_LABEL}",
                "color": 0xFF0000,
                "fields": fields,
            },
            "text": "\n".join(text_lines),
        }

    # ── Hourly Pulse ─────────────────────────────────────────────────────
    if tier == "hourly_pulse":
        coin = data.get("coin", "?")
        direction = data.get("direction", {})
        long_ex = direction.get("long_exchange", "?")
        short_ex = direction.get("short_exchange", "?")
        size = data.get("size", 0)
        price = data.get("entry_price", 0)
        notional = round(size * price, 2) if size and price else 0

        rate_map = {
            "hyperliquid": data.get("hl_rate", 0),
            "binance": data.get("bn_rate", 0),
        }
        long_rate = rate_map.get(long_ex, 0)
        short_rate = rate_map.get(short_ex, 0)
        current_apr = data.get("current_apr", 0)
        current_spread = data.get("current_spread", 0)
        delta_pct = data.get("delta_pct", 0)
        healthy = data.get("healthy", True)

        hl_bal = data.get("hl_balance", 0)
        bn_bal = data.get("bn_balance", 0)
        total = round(hl_bal + bn_bal, 2)

        pnl = data.get("pnl", 0)
        roi_pct = data.get("roi_pct", 0)

        health_icon = "✅" if healthy else "⚠️"
        long_label = long_ex.title()
        short_label = short_ex.title()

        fields = [
            # Row 1: 资产
            {"name": "HL", "value": f"${hl_bal:,.2f}", "inline": True},
            {"name": "Binance", "value": f"${bn_bal:,.2f}", "inline": True},
            {"name": "Total", "value": f"${total:,.2f}", "inline": True},
            # Row 2: 方向 + size
            {"name": "Long", "value": long_label, "inline": True},
            {"name": "Short", "value": short_label, "inline": True},
            {
                "name": "Size",
                "value": f"{size:g} {coin} (${notional:,.0f})",
                "inline": True,
            },
            # Row 3: 费率对齐（Long 所的费率 | Short 所的费率 | Spread）
            {
                "name": f"{long_label} Rate",
                "value": f"{long_rate:.4%}/8h",
                "inline": True,
            },
            {
                "name": f"{short_label} Rate",
                "value": f"{short_rate:.4%}/8h",
                "inline": True,
            },
            {
                "name": "Spread → APR",
                "value": f"{current_spread:.4%}/8h → {current_apr:.1f}% APR",
                "inline": True,
            },
        ]

        footer = f"PnL ${pnl:+,.2f} ({roi_pct:+.2f}%) · {health_icon} {'健康' if healthy else '异常'}"

        text_lines = [
            f"📊 **{coin} · {STRATEGY_LABEL} · 运行中**",
            f"💰 HL `${hl_bal:,.2f}` + BN `${bn_bal:,.2f}` = **`${total:,.2f}`**",
            f"📍 Long `{long_label}` / Short `{short_label}` | `{size:g} {coin}` (`${notional:,.0f}`)",
            f"📈 {long_label} `{long_rate:.4%}/8h` / {short_label} `{short_rate:.4%}/8h` → Spread `{current_spread:.4%}/8h` ({current_apr:.1f}% APR)",
            f"_{footer}_",
        ]

        return {
            "tier": "hourly_pulse",
            "discord": {
                "title": f"📊 {coin} · {STRATEGY_LABEL} · 运行中",
                "color": 0x808080,
                "fields": fields,
                "footer": {"text": footer},
            },
            "text": "\n".join(text_lines),
        }

    # ── Daily Report ─────────────────────────────────────────────────────
    if tier == "daily_report":
        coin = data.get("coin", "—")
        direction = data.get("direction", {})
        long_ex = direction.get("long_exchange", "?")
        short_ex = direction.get("short_exchange", "?")

        hl_bal = data.get("hl_balance", 0)
        bn_bal = data.get("bn_balance", 0)
        total = data.get("current_total_balance", round(hl_bal + bn_bal, 2))
        entry_total = data.get("entry_total_balance", 0)

        pnl = data.get("pnl", 0)
        roi_pct = data.get("roi_pct", 0)

        rate_map = {
            "hyperliquid": data.get("hl_rate", 0),
            "binance": data.get("bn_rate", 0),
        }
        long_rate = rate_map.get(long_ex, 0)
        short_rate = rate_map.get(short_ex, 0)
        current_apr = data.get("current_apr", 0)
        current_spread = data.get("current_spread", 0)
        has_position = data.get("has_position", False)

        entry_time = data.get("entry_time", "")
        hours_held = 0.0
        if entry_time:
            try:
                entry_dt = datetime.fromisoformat(entry_time)
                hours_held = (
                    datetime.now(timezone.utc) - entry_dt
                ).total_seconds() / 3600
            except (ValueError, TypeError):
                pass

        today = datetime.now(timezone.utc).date().isoformat()
        long_label = long_ex.title()
        short_label = short_ex.title()

        if has_position:
            fields = [
                # Row 1: 方向 + 时长
                {"name": "Long", "value": long_label, "inline": True},
                {"name": "Short", "value": short_label, "inline": True},
                {
                    "name": "持仓",
                    "value": f"{coin} · {hours_held:.1f}h",
                    "inline": True,
                },
                # Row 2: 费率对齐
                {
                    "name": f"{long_label} Rate",
                    "value": f"{long_rate:.4%}/8h",
                    "inline": True,
                },
                {
                    "name": f"{short_label} Rate",
                    "value": f"{short_rate:.4%}/8h",
                    "inline": True,
                },
                {
                    "name": "Spread → APR",
                    "value": f"{current_spread:.4%}/8h → {current_apr:.1f}% APR",
                    "inline": True,
                },
                # Row 3: 资产
                {"name": "HL", "value": f"${hl_bal:,.2f}", "inline": True},
                {
                    "name": "Binance",
                    "value": f"${bn_bal:,.2f}",
                    "inline": True,
                },
                {
                    "name": "Total",
                    "value": f"${total:,.2f}",
                    "inline": True,
                },
                # Row 4: PnL
                {
                    "name": "💵 PnL",
                    "value": f"${pnl:+,.2f} ({roi_pct:+.2f}%)",
                    "inline": True,
                },
            ]
            footer = f"本金 ${entry_total:,.0f} · 持仓 {hours_held:.1f}h"
        else:
            fields = [
                {"name": "状态", "value": "空仓观望中", "inline": True},
                {"name": "HL", "value": f"${hl_bal:,.2f}", "inline": True},
                {
                    "name": "Binance",
                    "value": f"${bn_bal:,.2f}",
                    "inline": True,
                },
                {
                    "name": "💰 总资产",
                    "value": f"${total:,.2f}",
                    "inline": True,
                },
            ]
            footer = "无持仓"

        text_lines = [
            f"📈 **日报 · {STRATEGY_LABEL} · {today}**",
            "",
        ]
        if has_position:
            text_lines += [
                "**持仓**",
                f"  `{coin}` | Long `{long_label}` / Short `{short_label}` | `{hours_held:.1f}h`",
                f"  {long_label} `{long_rate:.4%}/8h` / {short_label} `{short_rate:.4%}/8h` → Spread `{current_spread:.4%}/8h` ({current_apr:.1f}% APR)",
                "",
                "**资产**",
                f"  HL: `${hl_bal:,.2f}` | BN: `${bn_bal:,.2f}` | Total: `${total:,.2f}`",
                f"  PnL: `${pnl:+,.2f}` (`{roi_pct:+.2f}%`)",
            ]
        else:
            text_lines += [
                "**状态**: 空仓观望中",
                f"**资产**: HL `${hl_bal:,.2f}` | BN `${bn_bal:,.2f}` | Total `${total:,.2f}`",
            ]
        text_lines.append(f"\n_{footer}_")

        return {
            "tier": "daily_report",
            "discord": {
                "title": f"📈 日报 · {STRATEGY_LABEL} · {today}",
                "color": 0x3399FF,
                "fields": fields,
                "footer": {"text": footer},
            },
            "text": "\n".join(text_lines),
        }

    return None


# ── Notification sending ─────────────────────────────────────────────────────


def _send_telegram(text: str) -> bool:
    import urllib.error
    import urllib.request

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def _send_notification(notif: dict) -> None:
    """Send notification to Discord (embed) and Telegram (text)."""
    import urllib.error
    import urllib.request

    discord_ok = False
    token = _get_discord_token()
    embed = notif.get("discord", {})
    if token and DISCORD_CHANNEL_ID and embed:
        url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
        payload = {"embeds": [embed]}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://openclaw.ai, 1.0)",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            discord_ok = True
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            pass

    tg_ok = False
    text = notif.get("text", "")
    if text:
        tg_ok = _send_telegram(text)

    if not discord_ok and not tg_ok and (token or TELEGRAM_BOT_TOKEN):
        # Only log if credentials were configured but both failed
        pass


# ── Public API ───────────────────────────────────────────────────────────────


def emit(event_type: str, data: dict, *, notify: bool = False, tier: str = "") -> None:
    """输出一行 JSON 事件到 stdout，并按 tier 推送通知。

    Args:
        event_type: 事件类型（tick, report, position_opened 等）
        data: 事件数据
        notify: 是否标记为需要通知
        tier: 通知级别（trade_alert, risk_alert, hourly_pulse, daily_report）
              为空则不推送
    """
    payload = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "notify": notify or bool(tier),
        **data,
    }
    if tier:
        notif = _build_notification(tier, {**data, "type": event_type})
        if notif:
            payload["notification"] = notif
            _send_notification(notif)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def emit_error(context: str, error: Exception, *, notify: bool = False) -> None:
    data = {
        "context": context,
        "error": str(error),
        "traceback": traceback.format_exc(),
    }
    tier = "risk_alert" if notify else ""
    emit("error", data, notify=notify, tier=tier)
