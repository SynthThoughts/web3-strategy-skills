#!/usr/bin/env python3
"""Multi-strategy trading dashboard generator.

Usage:
    # Config-driven (recommended):
    python3 generate_dashboard.py --config dashboard_config.json --out dashboard.html

    # Legacy single-strategy mode (backward compatible):
    python3 generate_dashboard.py --state grid_state_v4.json --log /tmp/grid_bot_cron.log --out dashboard.html

Config format (dashboard_config.json):
    {
        "chart_interval": "5m",
        "chart_hours": 24,
        "strategies": [
            {
                "id": "eth-grid",
                "name": "ETH 网格交易",
                "type": "grid",
                "inst_id": "ETH-USDT",
                "state_file": "/path/to/grid_state_v4.json",
                "log_file": "/tmp/grid_bot_cron.log"
            }
        ]
    }

Global settings (chart_interval, chart_hours, okx_api_url) serve as defaults
and can be overridden per strategy. Per-strategy inst_id sets the trading pair.

Produces a self-contained HTML dashboard styled after the OKX Onchain OS design language.
Each strategy renders as its own panel. New strategy types can be added by implementing
a render function and registering it in STRATEGY_RENDERERS.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# ── Configurable defaults (can be overridden via dashboard_config.json) ──────
DEFAULT_OKX_API_URL = "https://www.okx.com/api/v5/market/candles"
DEFAULT_INST_ID = "ETH-USDT"
DEFAULT_CHART_INTERVAL = "5m"
DEFAULT_CHART_HOURS = 24

# Mapping from bar string to minutes for TARGET_POINTS calculation
_BAR_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "2H": 120,
    "4H": 240,
    "6H": 360,
    "12H": 720,
    "1D": 1440,
}


def _calc_target_points(
    chart_hours: int = DEFAULT_CHART_HOURS, chart_interval: str = DEFAULT_CHART_INTERVAL
) -> int:
    """Calculate number of data points needed for the chart time window."""
    minutes_per_bar = _BAR_MINUTES.get(chart_interval, 5)
    return max(1, (chart_hours * 60) // minutes_per_bar)


TARGET_POINTS = _calc_target_points()  # default: 288 (24h at 5m)


# ══════════════════════════════════════════════════════════════════════════════
# Data extraction (shared utilities)
# ══════════════════════════════════════════════════════════════════════════════


def fetch_kline_prices(
    inst_id: str = DEFAULT_INST_ID,
    bar: str = DEFAULT_CHART_INTERVAL,
    limit: int = TARGET_POINTS,
    api_url: str = DEFAULT_OKX_API_URL,
) -> list[float]:
    """Fetch close prices from OKX public API (no auth needed).

    Returns oldest-first list of close prices, or empty list on failure.
    """
    url = f"{api_url}?instId={inst_id}&bar={bar}&limit={limit}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "strategy-dashboard/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        candles = data.get("data", [])
        # OKX returns newest-first: [ts, o, h, l, c, vol, ...]
        prices = [float(c[4]) for c in reversed(candles)]
        return prices
    except Exception:
        return []


def ensure_24h_prices(
    state_prices: list[float],
    inst_id: str = DEFAULT_INST_ID,
    bar: str = DEFAULT_CHART_INTERVAL,
    target_points: int = TARGET_POINTS,
    api_url: str = DEFAULT_OKX_API_URL,
) -> list[float]:
    """Ensure we have enough price data by backfilling from OKX API if needed."""
    if len(state_prices) >= target_points:
        return state_prices[-target_points:]

    gap = target_points - len(state_prices)
    kline_prices = fetch_kline_prices(inst_id, bar, target_points, api_url)
    if not kline_prices:
        return state_prices  # API failed, use what we have

    if len(state_prices) == 0:
        return kline_prices[-target_points:]

    # Backfill: take older kline prices to fill the gap, then append state prices
    # State prices are more accurate (actual tick prices), so they take priority
    backfill = kline_prices[:gap] if len(kline_prices) > gap else kline_prices
    combined = backfill + state_prices
    return combined[-target_points:]


def load_state(path: Path) -> dict:
    if not path.exists():
        print(f"State file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text())


def parse_log_jsons(path: Path, max_entries: int = 200) -> list[dict]:
    """Extract ---JSON--- blocks from cron log."""
    if not path.exists():
        return []
    text = path.read_text()
    blocks: list[dict] = []
    parts = text.split("---JSON---")
    for part in parts[1:]:  # skip text before first marker
        # Find the JSON object (starts with { ends with })
        brace_start = part.find("{")
        if brace_start == -1:
            continue
        depth = 0
        end = brace_start
        for i, ch in enumerate(part[brace_start:], brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            obj = json.loads(part[brace_start:end])
            blocks.append(obj)
        except json.JSONDecodeError:
            continue
    return blocks[-max_entries:]


def parse_log_events(path: Path) -> list[dict]:
    """Extract trade events, grid recalibrations, and stops from log."""
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text().splitlines():
        # Timestamp pattern: [2026-03-19 18:20:02]
        ts_match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        if not ts_match:
            continue
        ts = ts_match.group(1)

        if "TRADE EXECUTED" in line or "EXECUTED" in line.upper():
            events.append({"time": ts, "type": "trade", "detail": line})
        elif "STOP ACTIVE" in line:
            events.append({"time": ts, "type": "stop", "detail": line})
        elif "Grid set:" in line:
            events.append({"time": ts, "type": "grid_recal", "detail": line})
        elif "Trade failed" in line or "FAILED" in line:
            events.append({"time": ts, "type": "failure", "detail": line})
        elif "sell delay" in line or "trend_hold" in line or "sell_trail" in line:
            events.append({"time": ts, "type": "sell_delay", "detail": line})
        elif "SKIP" in line or "skip" in line:
            events.append({"time": ts, "type": "skip", "detail": line})

    return events[-100:]


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ══════════════════════════════════════════════════════════════════════════════
# Strategy: Grid Trading Panel Renderer
# ══════════════════════════════════════════════════════════════════════════════


def render_grid_strategy(strategy_cfg: dict) -> dict:
    """Render a grid trading strategy panel.

    Args:
        strategy_cfg: Strategy config with state_file, log_file, name, id.

    Returns:
        dict with keys: name, status_color, status_label, version, summary_items,
        chart_html, panels_html (the strategy-specific content).
    """
    state_path = Path(strategy_cfg["state_file"])
    log_path = Path(strategy_cfg.get("log_file", "/tmp/grid_bot_cron.log"))

    state = load_state(state_path)
    json_blocks = parse_log_jsons(log_path)
    events = parse_log_events(log_path)

    grid = state.get("grid", {})
    stats = state.get("stats", {})
    mtf = state.get("mtf_cache", {})
    kline = state.get("kline_cache", {})
    trades = state.get("trades", [])
    price_history = state.get("price_history", [])

    # Current values
    current_price = price_history[-1] if price_history else 0
    level_prices = grid.get("level_prices", [])
    current_level = state.get("current_level", 0)
    grid_range = grid.get("range", [0, 0])
    grid_center = grid.get("center", 0)
    step = grid.get("step", 0)
    buy_step_val = grid.get("buy_step", step)
    sell_step_val = grid.get("sell_step", step)
    is_asymmetric = abs(buy_step_val - sell_step_val) > 0.01

    # Portfolio: prefer JSON block, fallback to state balances
    latest = json_blocks[-1] if json_blocks else {}
    portfolio = latest.get("portfolio", {})
    total_usd = portfolio.get("total_usd", 0)
    eth_pct = portfolio.get("eth_pct", 0)

    if not total_usd:
        bal = state.get("last_balances", {})
        eth_bal = bal.get("eth", 0)
        usdc_bal = bal.get("usdc", 0)
        total_usd = eth_bal * current_price + usdc_bal
        eth_val = eth_bal * current_price
        eth_pct = eth_val / total_usd * 100 if total_usd > 0 else 0

    # PnL
    cost_basis = state.get("cost_basis", stats.get("initial_portfolio_usd", 0))
    pnl = total_usd - cost_basis if total_usd and cost_basis else 0
    grid_profit = stats.get("grid_profit", 0)

    # Trend
    trend = mtf.get("trend", "neutral")
    strength = mtf.get("strength", 0)
    momentum_1h = mtf.get("momentum_1h", 0)
    structure = mtf.get("structure", "ranging")
    atr_pct = kline.get("atr_pct", 0)
    ema_short = mtf.get("ema_short", 0)
    ema_medium = mtf.get("ema_medium", 0)
    ema_long = mtf.get("ema_long", 0)

    # Status
    status = latest.get("status", "unknown")
    stop_triggered = state.get("stop_triggered")
    version = latest.get("version", "4.2")

    # Trades summary
    buy_count = stats.get("buy_successes", 0)
    sell_count = stats.get("sell_successes", 0)
    total_trades = stats.get("total_trades", 0) or (buy_count + sell_count)

    # Price history for chart — ensure full window by backfilling from OKX API
    _inst_id = strategy_cfg.get("inst_id", DEFAULT_INST_ID)
    _bar = strategy_cfg.get("chart_interval", DEFAULT_CHART_INTERVAL)
    _chart_hours = strategy_cfg.get("chart_hours", DEFAULT_CHART_HOURS)
    _target_pts = _calc_target_points(_chart_hours, _bar)
    _api_url = strategy_cfg.get("okx_api_url", DEFAULT_OKX_API_URL)
    spark_prices = ensure_24h_prices(
        price_history, _inst_id, _bar, _target_pts, _api_url
    )

    # Combined grid + price chart SVG
    chart_svg = _build_grid_chart_svg(
        spark_prices,
        level_prices,
        current_price,
        current_level,
        grid_range,
        trades,
    )

    # Unified log (events + trades merged)
    log_html = _build_unified_log_html(events[-30:], trades)

    # Risk controls status
    risk_html = _build_risk_controls_html(state, mtf)

    trend_cn = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}.get(
        trend, trend
    )
    structure_cn = {"uptrend": "上升", "downtrend": "下降", "ranging": "震荡"}.get(
        structure, structure
    )

    # Compute strategy decision variables
    vol_mult_val = 1.5
    if strength > 0.3:
        if trend == "bullish":
            vol_mult_val = 1.5 + (3.0 - 1.5) * strength
        elif trend == "bearish":
            vol_mult_val = 1.5 - (1.5 - 1.0) * strength

    # Position sizing multipliers
    if trend == "bullish":
        buy_mult = 1.0 + strength * 0.5
        sell_mult = 1.0 - strength * 0.3
    elif trend == "bearish":
        buy_mult = 1.0 - strength * 0.3
        sell_mult = 1.0 + strength * 0.5
    else:
        buy_mult = 1.0
        sell_mult = 1.0

    # Position limits
    if trend == "bullish" and strength > 0.3:
        pos_max = 70 + int((80 - 70) * strength)
        pos_min = 30
    elif trend == "bearish" and strength > 0.3:
        pos_max = 70
        pos_min = 30 - int((30 - 25) * strength)
    else:
        pos_max = 70
        pos_min = 30

    # Pre-compute tooltip strings
    if strength > 0.3 and trend == "bullish":
        vol_mult_tip = (
            f"看涨强趋势: 倍数从 1.5 上调至 {vol_mult_val:.1f}x，网格变宽持仓待涨"
        )
    elif strength > 0.3 and trend == "bearish":
        vol_mult_tip = (
            f"看跌强趋势: 倍数从 1.5 下调至 {vol_mult_val:.1f}x，网格收窄加速出货"
        )
    else:
        vol_mult_tip = "趋势强度 ≤ 30%，使用基础倍数 1.5x"
    if trend == "bullish":
        sizing_tip = f"看涨: 买入 ×{buy_mult:.2f} (加仓) / 卖出 ×{sell_mult:.2f} (减仓)"
    elif trend == "bearish":
        sizing_tip = f"看跌: 买入 ×{buy_mult:.2f} (缩仓) / 卖出 ×{sell_mult:.2f} (加仓)"
    else:
        sizing_tip = "中性: 等量 ×1.00"

    # Asymmetric step display strings
    if is_asymmetric:
        step_tip = (
            f"Asymmetric Grid: 买入步长 ${buy_step_val:.1f} / 卖出步长 ${sell_step_val:.1f}。"
            + (
                "看涨: 买密卖疏(快速吸筹+持仓待涨)"
                if trend == "bullish"
                else "看跌: 卖密买疏(快速减仓+低位接盘)"
                if trend == "bearish"
                else "中性: 对称"
            )
        )
        step_value_html = (
            f'<span style="color:#c8ff00;">买${buy_step_val:.0f}</span>'
            f' <span style="color:#ff4c8b;">卖${sell_step_val:.0f}</span>'
        )
        step_footer = f"步长 买${buy_step_val:.0f}/卖${sell_step_val:.0f}"
    else:
        step_tip = "网格步长 = ATR × 波动率倍数 / (层数/2)。步长越大触发交易越少"
        step_value_html = (
            f"${step:,.1f} ({step / current_price * 100:.1f}%)"
            if current_price > 0
            else f"${step:,.1f}"
        )
        step_footer = (
            f"步长 ${step:,.1f} ({step / current_price * 100:.1f}%)"
            if current_price > 0
            else f"步长 ${step:,.1f}"
        )

    grid_pos_tip = (
        "价格在网格下半区，更可能触发买入"
        if current_level < grid.get("levels", 6) / 2
        else "价格在网格上半区，更可能触发卖出"
        if current_level > grid.get("levels", 6) / 2
        else "价格在网格中间"
    )
    grid_pos_label = (
        "偏低 ↓ 倾向买入"
        if current_level < grid.get("levels", 6) / 2
        else "偏高 ↑ 倾向卖出"
        if current_level > grid.get("levels", 6) / 2
        else "居中"
    )

    # Status determination
    status_color = "#c8ff00"
    status_label = "运行中"
    if stop_triggered:
        status_color = "#ff4c8b"
        status_label = "已停止"
    elif status == "no_trade":
        status_color = "#7b3fe4"
        status_label = "监控中"

    pnl_sign = "+" if pnl >= 0 else ""

    # ── Build summary items for portfolio strip ──
    summary_items = [
        {"label": "总资产", "value": f"${total_usd:,.2f}", "class": "lg"},
        {
            "label": "盈亏",
            "value": f"{pnl_sign}${abs(pnl):,.2f}",
            "class": "lime" if pnl >= 0 else "pink",
        },
        {"label": "网格利润", "value": f"${grid_profit:,.2f}"},
        {
            "label": "交易",
            "value": f'{total_trades} <span style="font-size:11px;color:#555;font-weight:400;">{buy_count}买 {sell_count}卖</span>',
        },
    ]

    position_html = (
        f'<div class="position-inline">'
        f'<span style="font-size:11px;color:#555;">ETH {eth_pct:.0f}%</span>'
        f'<div class="pos-bar-inline"><div class="pos-fill-inline" style="width:{eth_pct:.1f}%;"></div></div>'
        f'<span style="font-size:11px;color:#555;">USDC {100 - eth_pct:.0f}%</span>'
        f"</div>"
    )

    # ── Chart footer (trend badges + grid info) ──
    chart_footer = (
        f'<div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#aaa;">'
        f'<div style="display:flex;align-items:center;gap:10px;">'
        f'<span class="badge badge-{trend}" title="EMA(25m) ${ema_short:,.0f} / EMA(1h) ${ema_medium:,.0f} / EMA(4h) ${ema_long:,.0f}">{trend_cn}</span>'
        f'<span class="badge badge-{structure if structure != "ranging" else "ranging"}" title="8H 窗口结构检测">{structure_cn}</span>'
        f'<span title="趋势强度 = |EMA短-EMA长| / EMA长，归一化到 0-100%" style="color:{"#c8ff00" if trend == "bullish" else "#ff4c8b" if trend == "bearish" else "#aaa"};font-weight:600;">{strength:.0%}</span>'
        f'<span style="color:#555;">·</span>'
        f'<span title="网格中心价格，基于 20H EMA">中心 <span style="color:#ccc;font-weight:600;">${grid_center:,.0f}</span></span>'
        f'<span title="当前价格所在层级 / 总层数">层级 <span style="color:#ccc;font-weight:600;">L{current_level}/{grid.get("levels", 6)}</span></span>'
        f'<span title="{grid_pos_tip}" style="color:{"#c8ff00" if current_level < grid.get("levels", 6) / 2 else "#ff4c8b" if current_level > grid.get("levels", 6) / 2 else "#aaa"};">{grid_pos_label}</span>'
        f"</div>"
        f'<span style="color:#555;">{step_footer} · ATR {atr_pct:.2f}%</span>'
        f"</div>"
    )

    # ── Strategy decision panel ──
    decision_html = (
        f'<div class="panel">'
        f'<div class="panel-title">策略决策</div>'
        f'<div class="mtf-grid">'
        f'<div class="mtf-item" title="{step_tip}">'
        f'<span class="mtf-label">网格步长</span>'
        f'<span class="mtf-value">{step_value_html}</span>'
        f"</div>"
        f'<div class="mtf-item" title="1H K线真实波幅 (ATR)，步长计算的核心输入。ATR 越高网格越宽">'
        f'<span class="mtf-label">波动率</span>'
        f'<span class="mtf-value">{atr_pct:.2f}% ATR</span>'
        f"</div>"
        f'<div class="mtf-item" title="{vol_mult_tip}">'
        f'<span class="mtf-label">宽度倍数</span>'
        f'<span class="mtf-value" style="color:{"#c8ff00" if vol_mult_val > 1.5 else "#ff4c8b" if vol_mult_val < 1.5 else "#ccc"};">{vol_mult_val:.1f}x</span>'
        f"</div>"
        f'<div class="mtf-item" title="{sizing_tip}">'
        f'<span class="mtf-label">仓位倍数</span>'
        f'<span class="mtf-value"><span style="color:#c8ff00;">买{buy_mult:.2f}</span> <span style="color:#ff4c8b;">卖{sell_mult:.2f}</span></span>'
        f"</div>"
        f'<div class="mtf-item" title="ETH 持仓上限（超过阻止买入）和下限（低于阻止卖出），趋势强度 > 30% 时偏移">'
        f'<span class="mtf-label">仓位限制</span>'
        f'<span class="mtf-value">{pos_min}% – {pos_max}%</span>'
        f"</div>"
        f'<div class="mtf-item" title="1H 动量 > 0.5% 且看涨时阻止卖出；30min 跌幅 > 2% 时阻止买入">'
        f'<span class="mtf-label">动量</span>'
        f'<span class="mtf-value" style="color:{"#c8ff00" if momentum_1h > 0 else "#ff4c8b" if momentum_1h < 0 else "#ccc"};">{"+" if momentum_1h > 0 else ""}{momentum_1h:.2f}% 1H</span>'
        f"</div>"
        f"</div>"
        f'<div style="margin-top:20px;">'
        f'<div class="panel-title">风控状态</div>'
        f"{risk_html}"
        f"</div>"
        f"</div>"
    )

    # ── Log panel ──
    log_panel_html = (
        f'<div class="panel">'
        f'<div class="panel-title">活动日志</div>'
        f'<div class="events">{log_html}</div>'
        f"</div>"
    )

    return {
        "id": strategy_cfg.get("id", "grid"),
        "name": strategy_cfg.get("name", "网格交易"),
        "version": version,
        "status_color": status_color,
        "status_label": status_label,
        "summary_items": summary_items,
        "position_html": position_html,
        "chart_svg": chart_svg,
        "chart_footer": chart_footer,
        "left_panel": decision_html,
        "right_panel": log_panel_html,
    }


# ── Grid chart SVG ──


def _build_grid_chart_svg(
    prices: list[float],
    level_prices: list[float],
    current_price: float,
    current_level: int,
    grid_range: list[float],
    trades: list[dict] | None = None,
) -> str:
    """Build a combined grid levels + price chart SVG."""
    if not level_prices:
        return (
            '<svg viewBox="0 0 600 320"><text x="300" y="160" fill="#555" '
            'text-anchor="middle">暂无网格数据</text></svg>'
        )

    w, h = 960, 300
    pad_x, pad_y = 56, 16
    inner_w = w - 2 * pad_x
    inner_h = h - 2 * pad_y

    # Unified Y-axis: cover grid range + price history
    all_prices = list(level_prices) + [current_price]
    if prices:
        all_prices.extend(prices)
    price_min = min(all_prices) * 0.997
    price_max = max(all_prices) * 1.003
    price_span = price_max - price_min if price_max > price_min else 1

    def y_for(p: float) -> float:
        return pad_y + inner_h - (p - price_min) / price_span * inner_h

    elements: list[str] = []

    # ── Defs: gradients ──
    line_color = "#c8ff00"
    elements.append(f"""<defs>
      <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="{line_color}" stop-opacity="0.15"/>
        <stop offset="100%" stop-color="{line_color}" stop-opacity="0"/>
      </linearGradient>
      <clipPath id="chartClip">
        <rect x="{pad_x}" y="{pad_y}" width="{inner_w}" height="{inner_h}"/>
      </clipPath>
    </defs>""")

    # ── Grid level bands (alternating subtle fills) ──
    for i in range(len(level_prices) - 1):
        y_top = y_for(level_prices[i + 1])
        y_bot = y_for(level_prices[i])
        fill_opacity = "0.04" if i % 2 == 0 else "0.02"
        elements.append(
            f'<rect x="{pad_x}" y="{y_top:.1f}" width="{inner_w}" '
            f'height="{max(y_bot - y_top, 0):.1f}" fill="#7b3fe4" '
            f'fill-opacity="{fill_opacity}"/>'
        )

    # ── Grid level lines + labels ──
    for i, lp in enumerate(level_prices):
        y = y_for(lp)
        is_active = i == current_level or i == current_level + 1
        opacity = "0.35" if is_active else "0.12"
        stroke_w = "1" if is_active else "0.5"
        elements.append(
            f'<line x1="{pad_x}" y1="{y:.1f}" x2="{w - pad_x}" y2="{y:.1f}" '
            f'stroke="#7b3fe4" stroke-opacity="{opacity}" '
            f'stroke-width="{stroke_w}" stroke-dasharray="3,5"/>'
        )
        # Left: price label
        label_color = "#7b3fe4" if is_active else "#444"
        label_weight = "600" if is_active else "400"
        elements.append(
            f'<text x="{pad_x - 6}" y="{y + 3:.1f}" fill="{label_color}" '
            f'font-size="10" font-weight="{label_weight}" '
            f'text-anchor="end" font-family="Inter,monospace">${lp:,.0f}</text>'
        )
        # Right: level badge
        badge_bg = "rgba(123,63,228,0.15)" if is_active else "rgba(255,255,255,0.03)"
        badge_color = "#7b3fe4" if is_active else "#444"
        elements.append(
            f'<rect x="{w - pad_x + 4}" y="{y - 8:.1f}" width="28" height="16" '
            f'rx="4" fill="{badge_bg}"/>'
        )
        elements.append(
            f'<text x="{w - pad_x + 18}" y="{y + 3:.1f}" fill="{badge_color}" '
            f'font-size="9" font-weight="600" text-anchor="middle" '
            f'font-family="Inter,monospace">L{i}</text>'
        )

    # ── Price curve (clipped to chart area) ──
    if prices and len(prices) >= 2:
        points = []
        for i, p in enumerate(prices):
            x = pad_x + (i / (len(prices) - 1)) * inner_w
            y = y_for(p)
            points.append(f"{x:.1f},{y:.1f}")

        polyline = " ".join(points)
        # Area fill
        fill_points = (
            polyline
            + f" {pad_x + inner_w:.1f},{pad_y + inner_h:.1f}"
            + f" {pad_x:.1f},{pad_y + inner_h:.1f}"
        )
        elements.append(
            f'<g clip-path="url(#chartClip)">'
            f'<polygon points="{fill_points}" fill="url(#areaGrad)"/>'
            f'<polyline points="{polyline}" fill="none" stroke="{line_color}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f"</g>"
        )

        # End dot (current price)
        last_x = float(points[-1].split(",")[0])
        last_y = float(points[-1].split(",")[1])
        elements.append(
            f'<circle cx="{last_x}" cy="{last_y}" r="3" fill="{line_color}"/>'
        )
        elements.append(
            f'<circle cx="{last_x}" cy="{last_y}" r="7" fill="{line_color}" '
            f'fill-opacity="0.2"/>'
        )

    # ── Current price label (right-aligned pill) ──
    y_price = y_for(current_price)
    # Horizontal dotted line at current price
    elements.append(
        f'<line x1="{pad_x}" y1="{y_price:.1f}" x2="{w - pad_x}" y2="{y_price:.1f}" '
        f'stroke="#fff" stroke-width="0.5" stroke-opacity="0.3" stroke-dasharray="2,3"/>'
    )
    # Price pill on left
    pill_w = 52
    elements.append(
        f'<rect x="0" y="{y_price - 10:.1f}" width="{pill_w}" height="20" '
        f'rx="4" fill="#c8ff00" fill-opacity="0.12"/>'
    )
    elements.append(
        f'<text x="{pill_w / 2}" y="{y_price + 4:.1f}" fill="#c8ff00" '
        f'font-size="10" font-weight="700" text-anchor="middle" '
        f'font-family="Inter,monospace">${current_price:,.0f}</text>'
    )

    # ── Time axis hint ──
    n_prices = len(prices) if prices else 0
    if n_prices > 0:
        hours = n_prices * 5 / 60
        elements.append(
            f'<text x="{pad_x}" y="{h - 2}" fill="#333" font-size="9" '
            f'font-family="Inter,monospace">{hours:.0f}小时前</text>'
        )
        elements.append(
            f'<text x="{w - pad_x}" y="{h - 2}" fill="#333" font-size="9" '
            f'text-anchor="end" font-family="Inter,monospace">现在</text>'
        )

    # ── Trade markers (BUY ▲ / SELL ▼) ──
    if trades and prices and len(prices) >= 2:
        n = len(prices)
        now_ts = datetime.now(timezone.utc).timestamp()
        span_seconds = (n - 1) * 5 * 60
        start_ts = now_ts - span_seconds

        for t in trades:
            try:
                t_time = t.get("time", "")
                t_dir = t.get("direction", "")
                t_price = float(t.get("price", 0))
                if not t_time or not t_price:
                    continue

                # Parse ISO timestamp (trades use local time without tz)
                if t_time.endswith("Z"):
                    t_time = t_time[:-1] + "+00:00"
                trade_dt = datetime.fromisoformat(t_time)
                if trade_dt.tzinfo is None:
                    import time as _time

                    trade_ts = _time.mktime(trade_dt.timetuple())
                else:
                    trade_ts = trade_dt.timestamp()

                # Skip trades outside the chart timespan
                if trade_ts < start_ts or trade_ts > now_ts:
                    continue

                # Map timestamp to nearest price curve index for x alignment
                frac = (trade_ts - start_ts) / span_seconds if span_seconds > 0 else 1
                idx = round(frac * (n - 1))
                idx = max(0, min(n - 1, idx))
                # Use index-based x so marker sits exactly on the curve
                tx = pad_x + idx / (n - 1) * inner_w
                # Use the curve's actual price at that index for y alignment
                curve_price = prices[idx]
                ty = y_for(curve_price)

                if t_dir == "BUY":
                    elements.append(
                        f'<polygon points="{tx:.1f},{ty - 8:.1f} '
                        f'{tx - 5:.1f},{ty + 2:.1f} {tx + 5:.1f},{ty + 2:.1f}" '
                        f'fill="#c8ff00" fill-opacity="0.9"/>'
                    )
                elif t_dir == "SELL":
                    elements.append(
                        f'<polygon points="{tx:.1f},{ty + 8:.1f} '
                        f'{tx - 5:.1f},{ty - 2:.1f} {tx + 5:.1f},{ty - 2:.1f}" '
                        f'fill="#ff4c8b" fill-opacity="0.9"/>'
                    )

                # Price label next to marker (show actual trade price)
                label_y = ty - 12 if t_dir == "BUY" else ty + 16
                label_color = "#c8ff00" if t_dir == "BUY" else "#ff4c8b"
                elements.append(
                    f'<text x="{tx:.1f}" y="{label_y:.1f}" fill="{label_color}" '
                    f'font-size="8" font-weight="600" text-anchor="middle" '
                    f'font-family="Inter,monospace">${t_price:,.0f}</text>'
                )
            except (ValueError, TypeError):
                continue

    svg = (
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        + "".join(elements)
        + "</svg>"
    )
    return svg


# ── Unified log ──


def _build_unified_log_html(events: list[dict], trades: list[dict]) -> str:
    """Merge events and trades into a single timeline, sorted by time descending."""
    type_colors = {
        "trade": "#c8ff00",
        "stop": "#ff4c8b",
        "grid_recal": "#7b3fe4",
        "failure": "#ff4c8b",
        "sell_delay": "#f0a030",
        "skip": "#555",
        "buy": "#c8ff00",
        "sell": "#ff4c8b",
    }
    tag_labels = {
        "trade": "交易",
        "stop": "停止",
        "grid_recal": "校准",
        "failure": "失败",
        "sell_delay": "延迟",
        "skip": "跳过",
        "buy": "买入",
        "sell": "卖出",
    }

    entries: list[tuple[str, str, str, str, str]] = []

    for ev in events:
        ts = ev["time"]
        time_short = ts.split(" ")[-1][:5] if " " in ts else ts[:5]
        detail = ev["detail"]
        detail = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*", "", detail)
        etype = ev["type"]
        color = type_colors.get(etype, "#555")
        tag = tag_labels.get(etype, etype)
        entries.append((ts, time_short, tag, color, detail))

    for t in trades:
        ts = t.get("time", "")
        time_short = ts[11:16] if len(ts) >= 16 else ts[:5]
        direction = t.get("direction", "?")
        price = t.get("price", 0)
        amount_usd = t.get("amount_usd", 0)
        grid_from = t.get("grid_from", "?")
        grid_to = t.get("grid_to", "?")
        tx_hash = t.get("tx", "") or t.get("tx_hash", "")
        tx_short = tx_hash[:10] + "…" if tx_hash else ""

        tag_key = direction.lower()
        color = type_colors.get(tag_key, "#888")
        tag = tag_labels.get(tag_key, direction)
        detail = f"${price:,.2f} · ${amount_usd:,.2f} · L{grid_from}→L{grid_to}"
        if tx_short:
            detail += f" · {tx_short}"
        entries.append((ts, time_short, tag, color, detail))

    if not entries:
        return '<div style="color:#555;font-size:12px;padding:12px;">暂无记录</div>'

    entries.sort(key=lambda x: x[0], reverse=True)

    rows = []
    for _sort_key, time_short, tag, color, detail in entries[:30]:
        rows.append(
            f'<div class="event-row">'
            f'<span class="event-time">{time_short}</span>'
            f'<span class="event-tag" style="background:{color}20;color:{color};">{tag}</span>'
            f'<span class="event-text">{_escape_html(detail)}</span>'
            f"</div>"
        )

    return "\n".join(rows)


# ── Risk controls ──


def _build_risk_controls_html(state: dict, mtf: dict) -> str:
    stats = state.get("stats", {})
    price_history = state.get("price_history", [])
    items: list[tuple[str, str, str, str, str]] = []

    # 1. Stop-loss
    stop = state.get("stop_triggered")
    cost_basis = state.get("cost_basis", 0)
    total_usd = state.get("last_balances", {}).get("eth", 0) * (
        price_history[-1] if price_history else 0
    ) + state.get("last_balances", {}).get("usdc", 0)
    pnl_pct = (total_usd - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0
    if stop:
        items.append(
            (
                "🛑",
                "止损",
                str(stop),
                "risk-alert",
                f"组合跌破成本 15% 触发硬止损，需手动 resume-trading 恢复。"
                f"当前 PnL {pnl_pct:+.2f}%，成本 ${cost_basis:,.2f}",
            )
        )
    else:
        items.append(
            (
                "🔒",
                "止损",
                f"{pnl_pct:+.1f}%",
                "risk-ok",
                f"组合跌破成本 15% 触发硬止损。"
                f"当前 ${total_usd:,.2f} vs 成本 ${cost_basis:,.2f}，距触发 {15 + pnl_pct:.1f}%",
            )
        )

    # 2. Trailing stop
    peak = stats.get("portfolio_peak_usd", 0)
    drawdown_pct = (peak - total_usd) / peak * 100 if peak > 0 else 0
    dd_class = (
        "risk-alert"
        if drawdown_pct >= 10
        else ("risk-warn" if drawdown_pct >= 5 else "risk-ok")
    )
    items.append(
        (
            "📉",
            "回撤",
            f"-{drawdown_pct:.2f}%",
            dd_class,
            f"Trailing Stop: 从峰值回撤超 10% 触发追踪止损。"
            f"峰值 ${peak:,.2f}，当前 ${total_usd:,.2f}，距触发 {10 - drawdown_pct:.1f}%",
        )
    )

    # 3. Circuit breaker
    consec_err = stats.get("consecutive_errors", 0)
    if consec_err >= 5:
        items.append(
            (
                "🔌",
                "熔断 C.B.",
                f"已触发 ({consec_err})",
                "risk-alert",
                "Circuit Breaker: 连续 5 次错误暂停交易 1h。成功 tick 重置。当前已熔断",
            )
        )
    else:
        items.append(
            (
                "🔌",
                "熔断 C.B.",
                f"{consec_err}/5",
                "risk-ok",
                f"Circuit Breaker: 连续 5 次错误暂停交易 1h。当前 {consec_err} 次，距触发 {5 - consec_err} 次",
            )
        )

    # 4. Cooldown
    last_buy_t = state.get("last_trade_times", {}).get("BUY", "")
    last_sell_t = state.get("last_trade_times", {}).get("SELL", "")
    now = datetime.now()

    def _minutes_since(iso_str: str) -> float:
        if not iso_str:
            return 9999
        try:
            dt = datetime.fromisoformat(iso_str)
            return (now - dt).total_seconds() / 60
        except ValueError:
            return 9999

    buy_cd = _minutes_since(last_buy_t)
    sell_cd = _minutes_since(last_sell_t)
    buy_ready = buy_cd >= 30
    sell_ready = sell_cd >= 30
    if buy_ready and sell_ready:
        cd_val = "就绪"
        cd_class = "risk-ok"
    else:
        parts = []
        if not buy_ready:
            parts.append(f"买 {30 - buy_cd:.0f}m")
        if not sell_ready:
            parts.append(f"卖 {30 - sell_cd:.0f}m")
        cd_val = " / ".join(parts)
        cd_class = "risk-warn"
    items.append(
        (
            "⏱",
            "冷却",
            cd_val,
            cd_class,
            f"同方向交易间隔 ≥ 30 分钟。"
            f"上次买 {buy_cd:.0f}m 前，上次卖 {sell_cd:.0f}m 前",
        )
    )

    # 5. Consecutive same-direction limit
    consec_dir = state.get("consecutive_same_dir", 0)
    items.append(
        (
            "↔",
            "连续同向",
            f"{consec_dir}/3",
            "risk-warn" if consec_dir >= 2 else "risk-ok",
            f"同方向最多连续 3 笔。超限后需换向、等 1h 或网格校准后重置。当前 {consec_dir}",
        )
    )

    # 6. Rapid drop protection
    if len(price_history) >= 6:
        recent_6 = price_history[-6:]
        price_now = price_history[-1]
        drop_pct = (max(recent_6) - price_now) / max(recent_6) * 100
    else:
        drop_pct = 0
    drop_active = drop_pct > 2
    items.append(
        (
            "⛑",
            "闪崩保护",
            f"{drop_pct:.1f}%" if drop_pct > 0.1 else "正常",
            "risk-warn" if drop_active else "risk-ok",
            f"Flash Crash Protection: 30 分钟内跌幅 > 2% 时阻止买入。"
            f"当前 30min 跌幅 {drop_pct:.2f}%，{'已触发阻止买入' if drop_active else '未触发'}",
        )
    )

    # 7. Sell momentum protection
    momentum = mtf.get("momentum_1h", 0)
    trend = mtf.get("trend", "neutral")
    sell_blocked = trend == "bullish" and momentum > 0.5
    if sell_blocked:
        items.append(
            (
                "🛡",
                "动量过滤",
                f"生效 {momentum:+.2f}%",
                "risk-warn",
                f"Momentum Filter: 看涨 + 1H 动量 {momentum:+.2f}% > 0.5%，跳过卖出。"
                "趋势转弱或动量回落后自动解除",
            )
        )
    else:
        items.append(
            (
                "🛡",
                "动量过滤",
                "待命",
                "risk-ok",
                f"Momentum Filter: 看涨趋势下 1H 动量 > 0.5% 时跳过卖出。"
                f"当前趋势 {trend}，动量 {momentum:+.2f}%",
            )
        )

    # 8. Position limits
    strength = mtf.get("strength", 0)
    eth_balance = state.get("last_balances", {}).get("eth", 0)
    usdc_balance = state.get("last_balances", {}).get("usdc", 0)
    cur_price = price_history[-1] if price_history else 0
    eth_val = eth_balance * cur_price
    eth_pct_now = (
        eth_val / (eth_val + usdc_balance) * 100 if (eth_val + usdc_balance) > 0 else 0
    )
    if trend == "bullish" and strength > 0.3:
        pos_max = 70 + int((80 - 70) * strength)
        pos_min = 30
    elif trend == "bearish" and strength > 0.3:
        pos_max = 70
        pos_min = 30 - int((30 - 25) * strength)
    else:
        pos_max = 70
        pos_min = 30
    buy_blocked = eth_pct_now > pos_max
    sell_blocked_pos = eth_pct_now < pos_min
    if buy_blocked:
        pl_val = f"买入阻止 ({eth_pct_now:.0f}%>{pos_max:.0f}%)"
        pl_class = "risk-warn"
    elif sell_blocked_pos:
        pl_val = f"卖出阻止 ({eth_pct_now:.0f}%<{pos_min:.0f}%)"
        pl_class = "risk-warn"
    else:
        pl_val = f"ETH {eth_pct_now:.0f}%"
        pl_class = "risk-ok"
    items.append(
        (
            "📊",
            "仓位",
            pl_val,
            pl_class,
            f"ETH 占比 {eth_pct_now:.1f}%，允许范围 {pos_min:.0f}%-{pos_max:.0f}%。"
            f"{'超上限阻止买入' if buy_blocked else '低于下限阻止卖出' if sell_blocked_pos else '范围内正常交易'}",
        )
    )

    rows = []
    for icon, name, status_text, css_class, tooltip in items:
        rows.append(
            f'<div class="risk-item" title="{_escape_html(tooltip)}">'
            f'<span class="risk-icon">{icon}</span>'
            f'<span class="risk-name">{name}</span>'
            f'<span class="risk-status {css_class}">{_escape_html(status_text)}</span>'
            f"</div>"
        )
    return f'<div class="risk-grid">{"".join(rows)}</div>'


# ══════════════════════════════════════════════════════════════════════════════
# Strategy: CL LP (Concentrated Liquidity LP) Panel Renderer
# ══════════════════════════════════════════════════════════════════════════════


def _tick_to_price(tick: int) -> float:
    """Convert Uniswap V3 tick to price: price = 1.0001 ** tick."""
    return 1.0001**tick


def _build_cl_lp_chart_svg(
    price_history: list[float],
    rebalance_history: list[dict],
    position: dict,
    stats: dict,
) -> str:
    """Build a tick-range visualization SVG for CL LP strategy."""
    if not price_history:
        return (
            '<svg viewBox="0 0 960 300"><text x="480" y="150" fill="#555" '
            'text-anchor="middle">暂无价格数据</text></svg>'
        )

    w, h = 960, 300
    pad_x, pad_y = 56, 16
    inner_w = w - 2 * pad_x
    inner_h = h - 2 * pad_y

    # Collect all prices including range bounds for Y-axis scaling
    all_prices = list(price_history)
    lower_price = position.get("lower_price", 0)
    upper_price = position.get("upper_price", 0)
    if lower_price > 0:
        all_prices.append(lower_price)
    if upper_price > 0:
        all_prices.append(upper_price)

    for rb in rebalance_history:
        old_range = rb.get("old_range", [])
        new_range = rb.get("new_range", [])
        for ticks in [old_range, new_range]:
            if len(ticks) == 2 and ticks[0] is not None and ticks[1] is not None:
                all_prices.append(_tick_to_price(ticks[0]))
                all_prices.append(_tick_to_price(ticks[1]))

    price_min = min(all_prices) * 0.997
    price_max = max(all_prices) * 1.003
    price_span = price_max - price_min if price_max > price_min else 1

    def y_for(p: float) -> float:
        return pad_y + inner_h - (p - price_min) / price_span * inner_h

    elements: list[str] = []

    # ── Defs: gradients ──
    line_color = "#c8ff00"
    elements.append(f"""<defs>
      <linearGradient id="clAreaGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="{line_color}" stop-opacity="0.15"/>
        <stop offset="100%" stop-color="{line_color}" stop-opacity="0"/>
      </linearGradient>
      <clipPath id="clChartClip">
        <rect x="{pad_x}" y="{pad_y}" width="{inner_w}" height="{inner_h}"/>
      </clipPath>
    </defs>""")

    n = len(price_history)
    started_at = stats.get("started_at", "")
    last_check = stats.get("last_check", "")

    # Parse time boundaries for X-axis mapping
    try:
        t_start = datetime.fromisoformat(started_at)
        t_end = datetime.fromisoformat(last_check)
        total_seconds = max((t_end - t_start).total_seconds(), 1)
    except (ValueError, TypeError):
        t_start = None
        t_end = None
        total_seconds = max((n - 1) * 5 * 60, 1)  # fallback: 5min intervals

    def x_for_idx(i: int) -> float:
        return pad_x + (i / max(n - 1, 1)) * inner_w

    def x_for_time(iso_str: str) -> float:
        if t_start is None:
            return pad_x
        try:
            t = datetime.fromisoformat(iso_str)
            frac = (t - t_start).total_seconds() / total_seconds
            frac = max(0, min(1, frac))
            return pad_x + frac * inner_w
        except (ValueError, TypeError):
            return pad_x

    # ── Range segments from rebalance history ──
    # Build segments: each rebalance creates a range that lasts until the next
    segments: list[tuple[str, str, list[int]]] = []
    for i_rb, rb in enumerate(rebalance_history):
        t_rb = rb.get("time", "")
        new_range = rb.get("new_range", [])
        if len(new_range) != 2:
            continue
        # End time = next rebalance time or last_check
        if i_rb + 1 < len(rebalance_history):
            t_end_seg = rebalance_history[i_rb + 1].get("time", last_check)
        else:
            t_end_seg = last_check
        segments.append((t_rb, t_end_seg, new_range))

    for i_seg, (t_seg_start, t_seg_end, ticks) in enumerate(segments):
        x_start = x_for_time(t_seg_start)
        x_end = x_for_time(t_seg_end)
        p_lower = _tick_to_price(ticks[0])
        p_upper = _tick_to_price(ticks[1])
        y_top = y_for(max(p_lower, p_upper))
        y_bot = y_for(min(p_lower, p_upper))
        seg_w = max(x_end - x_start, 2)
        seg_h = max(y_bot - y_top, 1)

        is_current = i_seg == len(segments) - 1
        fill_opacity = "0.12" if is_current else "0.06"
        stroke_dash = "" if is_current else 'stroke-dasharray="4,3"'
        stroke_opacity = "0.5" if is_current else "0.2"

        elements.append(
            f'<rect x="{x_start:.1f}" y="{y_top:.1f}" width="{seg_w:.1f}" '
            f'height="{seg_h:.1f}" fill="#7b3fe4" fill-opacity="{fill_opacity}" '
            f'stroke="#7b3fe4" stroke-opacity="{stroke_opacity}" '
            f'stroke-width="1" {stroke_dash} rx="2"/>'
        )

    # ── Rebalance event vertical dashed lines ──
    for rb in rebalance_history:
        t_rb = rb.get("time", "")
        x_rb = x_for_time(t_rb)
        elements.append(
            f'<line x1="{x_rb:.1f}" y1="{pad_y}" x2="{x_rb:.1f}" '
            f'y2="{pad_y + inner_h}" stroke="#7b3fe4" stroke-opacity="0.25" '
            f'stroke-width="0.5" stroke-dasharray="3,4"/>'
        )

    # ── Current range bounds (horizontal dashed lines) ──
    if lower_price > 0 and upper_price > 0:
        for bound_p, label in [(upper_price, "上界"), (lower_price, "下界")]:
            y_b = y_for(bound_p)
            elements.append(
                f'<line x1="{pad_x}" y1="{y_b:.1f}" x2="{w - pad_x}" '
                f'y2="{y_b:.1f}" stroke="#7b3fe4" stroke-opacity="0.35" '
                f'stroke-width="1" stroke-dasharray="3,5"/>'
            )
            elements.append(
                f'<text x="{pad_x - 6}" y="{y_b + 3:.1f}" fill="#7b3fe4" '
                f'font-size="9" font-weight="600" text-anchor="end" '
                f'font-family="Inter,monospace">${bound_p:,.1f}</text>'
            )
            elements.append(
                f'<text x="{w - pad_x + 4}" y="{y_b + 3:.1f}" fill="#555" '
                f'font-size="8" font-family="Inter,monospace">{label}</text>'
            )

    # ── Price curve ──
    if n >= 2:
        points = []
        for i_p, p in enumerate(price_history):
            x = x_for_idx(i_p)
            y = y_for(p)
            points.append(f"{x:.1f},{y:.1f}")

        polyline = " ".join(points)
        fill_points = (
            polyline
            + f" {pad_x + inner_w:.1f},{pad_y + inner_h:.1f}"
            + f" {pad_x:.1f},{pad_y + inner_h:.1f}"
        )
        elements.append(
            f'<g clip-path="url(#clChartClip)">'
            f'<polygon points="{fill_points}" fill="url(#clAreaGrad)"/>'
            f'<polyline points="{polyline}" fill="none" stroke="{line_color}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f"</g>"
        )

        # End dot (current price)
        last_x = float(points[-1].split(",")[0])
        last_y = float(points[-1].split(",")[1])
        elements.append(
            f'<circle cx="{last_x}" cy="{last_y}" r="3" fill="{line_color}"/>'
        )
        elements.append(
            f'<circle cx="{last_x}" cy="{last_y}" r="7" fill="{line_color}" '
            f'fill-opacity="0.2"/>'
        )

    # ── Current price label ──
    current_price = price_history[-1] if price_history else 0
    if current_price > 0:
        y_price = y_for(current_price)
        elements.append(
            f'<line x1="{pad_x}" y1="{y_price:.1f}" x2="{w - pad_x}" '
            f'y2="{y_price:.1f}" stroke="#fff" stroke-width="0.5" '
            f'stroke-opacity="0.3" stroke-dasharray="2,3"/>'
        )
        pill_w = 52
        elements.append(
            f'<rect x="0" y="{y_price - 10:.1f}" width="{pill_w}" height="20" '
            f'rx="4" fill="#c8ff00" fill-opacity="0.12"/>'
        )
        elements.append(
            f'<text x="{pill_w / 2}" y="{y_price + 4:.1f}" fill="#c8ff00" '
            f'font-size="10" font-weight="700" text-anchor="middle" '
            f'font-family="Inter,monospace">${current_price:,.0f}</text>'
        )

    # ── Time axis hint ──
    if n > 0:
        hours = n * 5 / 60
        elements.append(
            f'<text x="{pad_x}" y="{h - 2}" fill="#333" font-size="9" '
            f'font-family="Inter,monospace">{hours:.0f}小时前</text>'
        )
        elements.append(
            f'<text x="{w - pad_x}" y="{h - 2}" fill="#333" font-size="9" '
            f'text-anchor="end" font-family="Inter,monospace">现在</text>'
        )

    # ── Y-axis price ticks ──
    n_ticks = 5
    for i_t in range(n_ticks + 1):
        p = price_min + (price_max - price_min) * i_t / n_ticks
        y = y_for(p)
        elements.append(
            f'<line x1="{pad_x}" y1="{y:.1f}" x2="{w - pad_x}" y2="{y:.1f}" '
            f'stroke="#fff" stroke-opacity="0.03" stroke-width="0.5"/>'
        )

    svg = (
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        + "".join(elements)
        + "</svg>"
    )
    return svg


def _build_cl_lp_risk_html(state: dict) -> str:
    """Build risk controls HTML for CL LP strategy."""
    stats = state.get("stats", {})
    errors = state.get("errors", {})
    rebalance_history = state.get("rebalance_history", [])
    items: list[tuple[str, str, str, str, str]] = []

    # 1. Stop-loss (15%)
    stop = state.get("stop_triggered")
    initial = stats.get("initial_portfolio_usd", 0)
    net_yield = stats.get("net_yield_usd", 0)
    pnl_pct = net_yield / initial * 100 if initial > 0 else 0
    if stop:
        items.append(
            (
                "🛑",
                "止损",
                str(stop),
                "risk-alert",
                f"组合跌破初始投入 15% 触发硬止损。"
                f"当前 PnL {pnl_pct:+.1f}%，初始 ${initial:,.2f}",
            )
        )
    else:
        items.append(
            (
                "🔒",
                "止损",
                f"{pnl_pct:+.1f}%",
                "risk-ok" if pnl_pct > -10 else "risk-warn",
                f"组合跌破初始投入 15% 触发硬止损。"
                f"初始 ${initial:,.2f}，净收益 ${net_yield:,.2f}，距触发 {15 + pnl_pct:.1f}%",
            )
        )

    # 2. Trailing stop (10% drawdown from peak)
    peak = stats.get("portfolio_peak_usd", 0)
    current_val = initial + net_yield if initial > 0 else 0
    drawdown_pct = (peak - current_val) / peak * 100 if peak > 0 else 0
    dd_class = (
        "risk-alert"
        if drawdown_pct >= 10
        else ("risk-warn" if drawdown_pct >= 5 else "risk-ok")
    )
    items.append(
        (
            "📉",
            "回撤",
            f"-{drawdown_pct:.2f}%",
            dd_class,
            f"Trailing Stop: 从峰值回撤超 10% 触发追踪止损。"
            f"峰值 ${peak:,.2f}，当前 ${current_val:,.2f}，距触发 {10 - drawdown_pct:.1f}%",
        )
    )

    # 3. Circuit breaker (consecutive errors / 5)
    consec_err = errors.get("consecutive", 0)
    cooldown = errors.get("cooldown_until")
    if consec_err >= 5:
        items.append(
            (
                "🔌",
                "熔断 C.B.",
                f"已触发 ({consec_err})",
                "risk-alert",
                f"连续 5 次错误暂停操作。当前 {consec_err} 次"
                + (f"，冷却至 {cooldown}" if cooldown else ""),
            )
        )
    else:
        items.append(
            (
                "🔌",
                "熔断 C.B.",
                f"{consec_err}/5",
                "risk-ok",
                f"连续 5 次错误暂停操作。当前 {consec_err} 次，距触发 {5 - consec_err} 次",
            )
        )

    # 4. IL limit (estimated_il_pct vs 5%)
    il_pct = stats.get("estimated_il_pct", 0)
    il_limit = 5.0
    il_class = (
        "risk-alert"
        if il_pct >= il_limit
        else ("risk-warn" if il_pct >= il_limit * 0.6 else "risk-ok")
    )
    items.append(
        (
            "📊",
            "IL 上限",
            f"{il_pct:.1f}%",
            il_class,
            f"无常损失超过 {il_limit:.0f}% 触发止损。"
            f"当前 IL {il_pct:.2f}%，距触发 {il_limit - il_pct:.1f}%",
        )
    )

    # 5. Rebalance frequency (actual/day vs 6/day cap)
    total_rebalances = stats.get("total_rebalances", 0)
    started_at = stats.get("started_at", "")
    try:
        dt_start = datetime.fromisoformat(started_at)
        days_running = max((datetime.now() - dt_start).total_seconds() / 86400, 0.01)
    except (ValueError, TypeError):
        days_running = 1
    rebal_per_day = total_rebalances / days_running
    freq_limit = 6.0
    freq_class = (
        "risk-alert"
        if rebal_per_day >= freq_limit
        else ("risk-warn" if rebal_per_day >= freq_limit * 0.7 else "risk-ok")
    )
    items.append(
        (
            "🔄",
            "调仓频率",
            f"{rebal_per_day:.1f}/天",
            freq_class,
            f"调仓频率上限 {freq_limit:.0f}/天。"
            f"当前 {rebal_per_day:.1f}/天，共 {total_rebalances} 次 / {days_running:.1f} 天",
        )
    )

    # 6. Position age (time since last rebalance vs 2h minimum)
    if rebalance_history:
        last_rb_time = rebalance_history[-1].get("time", "")
        try:
            dt_last_rb = datetime.fromisoformat(last_rb_time)
            age_hours = (datetime.now() - dt_last_rb).total_seconds() / 3600
        except (ValueError, TypeError):
            age_hours = 0
    else:
        age_hours = 0
    min_age = 2.0
    age_class = "risk-warn" if age_hours < min_age else "risk-ok"
    items.append(
        (
            "⏱",
            "头寸年龄",
            f"{age_hours:.1f}h",
            age_class,
            f"最小调仓间隔 {min_age:.0f}h。"
            f"距上次调仓 {age_hours:.1f}h，{'冷却中' if age_hours < min_age else '可调仓'}",
        )
    )

    rows = []
    for icon, name, status_text, css_class, tooltip in items:
        rows.append(
            f'<div class="risk-item" title="{_escape_html(tooltip)}">'
            f'<span class="risk-icon">{icon}</span>'
            f'<span class="risk-name">{name}</span>'
            f'<span class="risk-status {css_class}">{_escape_html(status_text)}</span>'
            f"</div>"
        )
    return f'<div class="risk-grid">{"".join(rows)}</div>'


def _build_cl_lp_log_html(rebalance_history: list[dict]) -> str:
    """Build rebalance log HTML for CL LP strategy."""
    type_colors = {
        "out_of_range": "#ff4c8b",
        "edge_proximity": "#f0a030",
        "vol_change": "#7b3fe4",
        "time_decay": "#555",
    }
    tag_labels = {
        "out_of_range": "越界",
        "edge_proximity": "边缘",
        "vol_change": "波动",
        "time_decay": "衰减",
    }

    if not rebalance_history:
        return '<div style="color:#555;font-size:12px;padding:12px;">暂无调仓记录</div>'

    rows = []
    for rb in reversed(rebalance_history[-30:]):
        t = rb.get("time", "")
        time_short = t[11:16] if len(t) >= 16 else t[:5]
        trigger = rb.get("trigger", "unknown")
        detail_dir = rb.get("detail", "")
        old_range = rb.get("old_range", [])
        new_range = rb.get("new_range", [])

        color = type_colors.get(trigger, "#555")
        tag = tag_labels.get(trigger, trigger)

        # Format range as prices
        detail_parts = []
        if detail_dir:
            detail_parts.append(detail_dir)
        if len(old_range) == 2 and len(new_range) == 2 and all(
            t is not None for t in old_range + new_range
        ):
            old_lo = _tick_to_price(old_range[0])
            old_hi = _tick_to_price(old_range[1])
            new_lo = _tick_to_price(new_range[0])
            new_hi = _tick_to_price(new_range[1])
            detail_parts.append(
                f"${old_lo:,.0f}-${old_hi:,.0f} → ${new_lo:,.0f}-${new_hi:,.0f}"
            )
        detail_text = " · ".join(detail_parts) if detail_parts else trigger

        rows.append(
            f'<div class="event-row">'
            f'<span class="event-time">{time_short}</span>'
            f'<span class="event-tag" style="background:{color}20;color:{color};">{tag}</span>'
            f'<span class="event-text">{_escape_html(detail_text)}</span>'
            f"</div>"
        )

    return "\n".join(rows)


def render_cl_lp_strategy(strategy_cfg: dict) -> dict:
    """Render a CL LP strategy panel with portfolio-centric layout.

    Returns dict with body_html for fully custom layout (bypasses standard assembly).
    """
    state_path = Path(strategy_cfg["state_file"])
    state = load_state(state_path)

    pool = state.get("pool", {})
    position = state.get("position", {})
    stats = state.get("stats", {})
    kline = state.get("kline_cache", {})
    errors = state.get("errors", {})
    price_history = state.get("price_history", [])
    rebalance_history = state.get("rebalance_history", [])
    stop_triggered = state.get("stop_triggered")

    # Current values
    current_price = price_history[-1] if price_history else 0
    lower_price = position.get("lower_price", 0)
    upper_price = position.get("upper_price", 0)
    atr_pct = kline.get("atr_pct", 0)
    time_in_range = stats.get("time_in_range_pct", 0)
    token_id = position.get("token_id", "")

    # Financial
    initial = stats.get("initial_portfolio_usd", 0)
    net_yield = stats.get("net_yield_usd", 0)
    fees_claimed = stats.get("total_fees_claimed_usd", 0)
    unclaimed_fee = stats.get("unclaimed_fee_usd", 0)
    fees = fees_claimed + unclaimed_fee
    gas = stats.get("total_gas_spent_usd", 0)
    strategy_earn = fees - gas
    # Total PnL = net_yield (from rebalances) + unclaimed fees - gas
    pnl_total = net_yield + unclaimed_fee - gas
    total_usd = initial + pnl_total if initial > 0 else 0
    price_movement = pnl_total - strategy_earn
    total_rebalances = stats.get("total_rebalances", 0)
    il_pct = stats.get("estimated_il_pct", 0)
    il_usd = initial * il_pct / 100 if initial > 0 else 0

    # APR
    started_at = stats.get("started_at", "")
    try:
        dt_start = datetime.fromisoformat(started_at)
        days_running = max((datetime.now() - dt_start).total_seconds() / 86400, 0.01)
    except (ValueError, TypeError):
        days_running = 1
    apr = (pnl_total / initial) * (365 / days_running) * 100 if initial > 0 else 0

    # Version & pool info
    version = str(state.get("version", "1"))
    chain = pool.get("chain", "base").upper()
    fee_tier = pool.get("fee_tier", 0)
    fee_display = f"{fee_tier * 100:.2f}%" if fee_tier else "0.30%"
    token0_sym = pool.get("token0", {}).get("symbol", "WETH")
    token1_sym = pool.get("token1", {}).get("symbol", "USDC")
    tick_spacing = pool.get("tick_spacing", 60)
    investment_id = pool.get("investment_id", "")

    # In-range status
    in_range = (
        lower_price <= current_price <= upper_price
        if current_price > 0 and lower_price > 0
        else False
    )

    # Status
    status_color = "#c8ff00"
    status_label = "Running"
    if stop_triggered:
        status_color = "#ff4c8b"
        status_label = "Stopped"
    elif not in_range:
        status_color = "#f0a030"
        status_label = "Out of Range"
    elif errors.get("consecutive", 0) >= 5:
        status_color = "#ff4c8b"
        status_label = "Circuit Break"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_time = datetime.now().strftime("%H:%M:%S")

    # PnL
    pnl = pnl_total
    pnl_pct = (pnl / initial * 100) if initial > 0 else 0
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_color = "#c8ff00" if pnl >= 0 else "#ff4c8b"

    # Range bar calculation
    range_span = upper_price - lower_price if upper_price > lower_price else 1
    padding = range_span * 0.15
    bar_lower = lower_price - padding
    bar_upper = upper_price + padding
    bar_span = bar_upper - bar_lower
    fill_left = (lower_price - bar_lower) / bar_span * 100
    fill_width = range_span / bar_span * 100
    if in_range and range_span > 0:
        dot_pct = (current_price - lower_price) / range_span * 100
    elif current_price < lower_price:
        dot_pct = 0
    else:
        dot_pct = 100
    dot_pct = max(2, min(98, dot_pct))

    # Risk + Log HTML (reuse existing helpers)
    risk_html = _build_cl_lp_risk_html(state)
    log_html = _build_cl_lp_log_html(rebalance_history)

    # ── Build full body HTML ──
    body_html = (
        # Header
        f'<div class="header">'
        f'<div class="header-left">'
        f'<div class="logo">{strategy_cfg.get("name", "V3 LP 调仓")}</div>'
        f'<span class="version">v{version}</span>'
        f"</div>"
        f'<div style="display:flex;align-items:center;gap:16px;">'
        f'<div class="status-badge">'
        f'<div class="status-dot" style="background:{status_color};'
        f'box-shadow:0 0 8px {status_color};"></div>'
        f'<span style="color:{status_color};">{status_label}</span>'
        f"</div>"
        f'<span class="timestamp">{now_str}</span>'
        f"</div>"
        f"</div>"
        # Portfolio Card
        f'<div class="lp-portfolio">'
        f'<div class="lp-port-col">'
        f'<div class="lp-section-label">CURRENT VALUE</div>'
        f'<div class="lp-big-num">${total_usd:,.2f}</div>'
        f'<div class="lp-detail-rows">'
        f'<div class="lp-row">'
        f'<span class="lp-row-label">Invested</span>'
        f'<span class="lp-row-val">${initial:,.2f}</span>'
        f"</div>"
        f'<div class="lp-row">'
        f'<span class="lp-row-label">Total PnL</span>'
        f'<span class="lp-row-val" style="color:{pnl_color};">'
        f"{pnl_sign}${abs(pnl):,.2f}"
        f' <span style="font-size:12px;">{pnl_sign}{pnl_pct:.2f}%</span></span>'
        f"</div>"
        f'<div class="lp-row lp-sub">'
        f'<span class="lp-row-label">Strategy Earn</span>'
        f'<span class="lp-row-val" style="color:#c8ff00;">'
        f"{'+' if strategy_earn >= 0 else ''}${abs(strategy_earn):,.2f}</span>"
        f"</div>"
        f'<div class="lp-row lp-sub">'
        f'<span class="lp-row-label">Price Movement</span>'
        f'<span class="lp-row-val" style="color:'
        f'{"#c8ff00" if price_movement >= 0 else "#ff4c8b"};">'
        f"{'+' if price_movement >= 0 else ''}${abs(price_movement):,.2f}</span>"
        f"</div>"
        f"</div>"
        f"</div>"
        # Strategy Detail column
        f'<div class="lp-port-col lp-port-right">'
        f'<div class="lp-section-label">STRATEGY DETAIL</div>'
        f'<div class="lp-big-num" style="color:{pnl_color};">'
        f"{pnl_sign}${abs(pnl):,.2f}"
        f' <span class="lp-apr">APR {apr:.1f}%</span></div>'
        f'<div class="lp-detail-rows">'
        f'<div class="lp-row">'
        f'<span class="lp-row-label">Earn Claimed</span>'
        f'<span class="lp-row-val" style="color:#c8ff00;">${fees_claimed:,.2f}</span>'
        f"</div>"
        f'<div class="lp-row">'
        f'<span class="lp-row-label">Unclaimed</span>'
        f'<span class="lp-row-val" style="color:#c8ff00;">${unclaimed_fee:,.2f}</span>'
        f"</div>"
        f'<div class="lp-row">'
        f'<span class="lp-row-label">Gas Costs</span>'
        f'<span class="lp-row-val" style="color:#ff4c8b;">-${gas:,.2f}</span>'
        f"</div>"
        f'<div class="lp-row">'
        f'<span class="lp-row-label">Impermanent Loss</span>'
        f'<span class="lp-row-val" style="color:#ff4c8b;">'
        f"${'-' if il_usd > 0 else ''}{il_usd:,.2f}</span>"
        f"</div>"
        f"</div>"
        f"</div>"
        f"</div>"
        # Active Strategy header card
        f'<div class="lp-section-title">Active Strategies</div>'
        f'<div class="lp-strat-card">'
        f'<div class="lp-strat-header">'
        f'<div style="display:flex;align-items:center;gap:10px;">'
        f'<span style="font-size:15px;font-weight:600;">'
        f"{chain} V3 Auto-Rebalancer</span>"
        f'<span class="badge badge-ranging">{chain}</span>'
        f'<span class="lp-status-pill" style="color:{status_color};'
        f"border-color:{status_color}40;"
        f'background:{status_color}10;">● {status_label}</span>'
        f"</div>"
        f'<span class="timestamp">{now_time}</span>'
        f"</div>"
        f'<div style="font-size:12px;color:#555;margin-top:4px;">'
        f"Dynamic range · Investment #{investment_id}</div>"
        f"</div>"
        # Info grid: Token ID, Liquidity, Range Bar
        f'<div class="lp-info-row">'
        f'<div class="lp-info-card">'
        f'<div class="lp-section-label">TOKEN ID</div>'
        f'<div class="lp-info-val">#{token_id}</div>'
        f"</div>"
        f'<div class="lp-info-card">'
        f'<div class="lp-section-label">LIQUIDITY</div>'
        f'<div class="lp-info-val" style="color:'
        f'{"#c8ff00" if in_range else "#ff4c8b"};">'
        f"{'Active' if in_range else 'Inactive'}</div>"
        f"</div>"
        f'<div class="lp-range-card">'
        f'<div class="lp-range-hdr">'
        f'<span style="color:#aaa;">⊙ #{token_id}'
        f" {token0_sym} / {token1_sym} ({fee_display})</span>"
        f'<span style="color:{"#c8ff00" if in_range else "#ff4c8b"};'
        f'font-weight:600;font-size:11px;">'
        f"● {'In Range' if in_range else 'Out of Range'}</span>"
        f"</div>"
        f'<div class="lp-range-track">'
        f'<div class="lp-range-fill" style="left:{fill_left:.1f}%;'
        f'width:{fill_width:.1f}%;">'
        f'<div class="lp-price-dot" style="left:{dot_pct:.1f}%;"></div>'
        f"</div>"
        f"</div>"
        f'<div class="lp-range-labels">'
        f"<span>${lower_price:,.0f}</span>"
        f'<span style="color:#c8ff00;font-weight:600;">'
        f"${current_price:,.2f}</span>"
        f"<span>${upper_price:,.0f}</span>"
        f"</div>"
        f'<div style="text-align:center;font-size:10px;color:#444;'
        f'margin-top:2px;">tickSpacing = {tick_spacing}</div>'
        f"</div>"
        f"</div>"
        # Token balances row
        f'<div class="lp-token-row">'
        f'<div class="lp-info-card">'
        f'<div class="lp-section-label">TIME IN RANGE</div>'
        f'<div class="lp-info-val" style="color:'
        f'{"#c8ff00" if time_in_range >= 95 else "#f0a030" if time_in_range >= 80 else "#ff4c8b"};">'
        f"{time_in_range:.1f}%</div>"
        f"</div>"
        f'<div class="lp-info-card">'
        f'<div class="lp-section-label">REBALANCES</div>'
        f'<div class="lp-info-val">{total_rebalances}</div>'
        f"</div>"
        f'<div class="lp-info-card">'
        f'<div class="lp-section-label">VOLATILITY</div>'
        f'<div class="lp-info-val">{atr_pct:.2f}% ATR</div>'
        f"</div>"
        f'<div class="lp-info-card">'
        f'<div class="lp-section-label">RUNNING</div>'
        f'<div class="lp-info-val">{days_running:.1f}d</div>'
        f"</div>"
        f"</div>"
        # Risk + Log two-column
        f'<div class="main-grid" style="margin-top:16px;">'
        f'<div class="panel">'
        f'<div class="panel-title">风控状态</div>'
        f"{risk_html}"
        f"</div>"
        f'<div class="panel">'
        f'<div class="panel-title">调仓日志</div>'
        f'<div class="events">{log_html}</div>'
        f"</div>"
        f"</div>"
    )

    return {
        "id": strategy_cfg.get("id", "cl-lp"),
        "name": strategy_cfg.get("name", "V3 LP 调仓"),
        "version": version,
        "status_color": status_color,
        "status_label": status_label,
        "body_html": body_html,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Strategy Registry — add new strategy renderers here
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_RENDERERS = {
    "grid": render_grid_strategy,
    "cl_lp": render_cl_lp_strategy,
}


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard Container (assembles multiple strategy panels)
# ══════════════════════════════════════════════════════════════════════════════


def generate_dashboard(strategies_config: list[dict]) -> str:
    """Generate the full dashboard HTML for multiple strategies."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Render all strategies
    panels: list[dict] = []
    for cfg in strategies_config:
        stype = cfg.get("type", "grid")
        renderer = STRATEGY_RENDERERS.get(stype)
        if not renderer:
            print(f"Unknown strategy type: {stype}, skipping", file=sys.stderr)
            continue
        try:
            panel = renderer(cfg)
            panels.append(panel)
        except Exception as e:
            print(
                f"Error rendering strategy {cfg.get('id', '?')}: {e}", file=sys.stderr
            )
            continue

    if not panels:
        return "<html><body><h1>No strategies configured</h1></body></html>"

    multi = len(panels) > 1

    # Build tab buttons (only if multiple strategies)
    tab_buttons = ""
    if multi:
        btns = []
        for i, p in enumerate(panels):
            active = "active" if i == 0 else ""
            btns.append(
                f'<button class="tab-btn {active}" onclick="switchTab(\'{p["id"]}\')" '
                f'data-tab="{p["id"]}">'
                f'<span class="status-dot" style="background:{p["status_color"]};'
                f"box-shadow:0 0 6px {p['status_color']};width:6px;height:6px;"
                f'border-radius:50%;display:inline-block;margin-right:6px;"></span>'
                f"{p['name']}"
                f"</button>"
            )
        tab_buttons = f'<div class="tab-bar">{"".join(btns)}</div>'

    # Build strategy sections
    sections = []
    for i, p in enumerate(panels):
        display = "" if i == 0 else "display:none;"
        sid = p["id"]

        if "body_html" in p:
            # Custom layout (e.g. CL LP strategy)
            section = (
                f'<div class="strategy-section" id="section-{sid}" '
                f'style="{display}">'
                f"{p['body_html']}"
                f"</div>"
            )
        else:
            # Standard layout (grid strategy)
            items_html = []
            for item in p["summary_items"]:
                cls = item.get("class", "")
                items_html.append(
                    f'<div class="port-item{"  primary" if cls == "lg" else ""}">'
                    f'<span class="port-label">{item["label"]}</span>'
                    f'<span class="port-val {cls}">{item["value"]}</span>'
                    f"</div>"
                )
            items_html.append(p.get("position_html", ""))
            strip = f'<div class="portfolio-strip">{"".join(items_html)}</div>'

            chart = (
                f'<div class="panel" style="margin-bottom:20px;">'
                f'<div class="grid-viz">{p["chart_svg"]}</div>'
                f"{p.get('chart_footer', '')}"
                f"</div>"
            )

            two_col = (
                f'<div class="main-grid">{p["left_panel"]}{p["right_panel"]}</div>'
            )

            section = (
                f'<div class="strategy-section" id="section-{sid}" '
                f'style="{display}">'
                f'<div class="header">'
                f'<div class="header-left">'
                f'<div class="logo">{p["name"]}</div>'
                f'<span class="version">v{p["version"]}</span>'
                f"</div>"
                f'<div style="display:flex;align-items:center;gap:16px;">'
                f'<div class="status-badge">'
                f'<div class="status-dot" style="background:{p["status_color"]};'
                f'box-shadow:0 0 8px {p["status_color"]};"></div>'
                f'<span style="color:{p["status_color"]};">'
                f"{p['status_label']}</span>"
                f"</div>"
                f'<span class="timestamp">{now_str}</span>'
                f"</div>"
                f"</div>"
                f"{strip}"
                f"{chart}"
                f"{two_col}"
                f"</div>"
            )
        sections.append(section)

    tab_js = ""
    if multi:
        tab_js = """
<script>
function switchTab(id) {
  document.querySelectorAll('.strategy-section').forEach(s => s.style.display = 'none');
  document.getElementById('section-' + id).style.display = '';
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelector('.tab-btn[data-tab="' + id + '"]').classList.add('active');
}
</script>"""

    tab_css = ""
    if multi:
        tab_css = """
  .tab-bar {
    display: flex; gap: 8px; margin-bottom: 20px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    padding-bottom: 12px;
  }
  .tab-btn {
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06);
    color: #888; font-size: 13px; font-weight: 500; padding: 8px 16px;
    border-radius: 8px; cursor: pointer; display: flex; align-items: center;
    transition: all 0.2s;
  }
  .tab-btn:hover { background: rgba(255,255,255,0.06); color: #ccc; }
  .tab-btn.active { background: rgba(123,63,228,0.15); color: #fff; border-color: #7b3fe4; }"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>策略看板</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: #000;
    color: #fff;
    min-height: 100vh;
    overflow-x: hidden;
  }}

  /* Aurora background */
  .bg-aurora {{
    position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: -1;
    background:
      radial-gradient(ellipse at 20% 50%, rgba(123, 63, 228, 0.15) 0%, transparent 50%),
      radial-gradient(ellipse at 80% 20%, rgba(200, 255, 0, 0.04) 0%, transparent 40%),
      radial-gradient(ellipse at 60% 80%, rgba(255, 76, 139, 0.06) 0%, transparent 50%),
      #000;
  }}

  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 20px; }}

  /* Header */
  .header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 0; margin-bottom: 24px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
  }}
  .header-left {{ display: flex; align-items: center; gap: 16px; }}
  .logo {{ font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }}
  .logo span {{ color: #c8ff00; }}
  .version {{
    font-size: 11px; color: #c8ff00; background: rgba(200,255,0,0.08);
    padding: 3px 8px; border-radius: 4px; font-weight: 500;
  }}
  .status-badge {{
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 600; letter-spacing: 0.5px;
  }}
  .status-dot {{
    width: 8px; height: 8px; border-radius: 50%;
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
  }}
  .timestamp {{ font-size: 11px; color: #555; }}

  /* Portfolio strip */
  .portfolio-strip {{
    display: flex; align-items: center; gap: 0; flex-wrap: wrap;
    padding: 0; margin-bottom: 20px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px;
    overflow: hidden;
  }}
  .port-item {{
    display: flex; flex-direction: column; gap: 4px;
    padding: 16px 24px;
    border-right: 1px solid rgba(255,255,255,0.06);
  }}
  .port-item:last-child {{ border-right: none; }}
  .port-item.primary {{ padding: 16px 32px; min-width: 160px; }}
  .port-label {{ font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: 0.5px; }}
  .port-val {{ font-size: 15px; font-weight: 600; white-space: nowrap; }}
  .port-val.lg {{ font-size: 22px; letter-spacing: -0.5px; }}
  .port-val.lime {{ color: #c8ff00; }}
  .port-val.pink {{ color: #ff4c8b; }}
  .position-inline {{
    display: flex; align-items: center; gap: 8px; margin-left: auto;
    padding: 16px 24px;
  }}
  .pos-bar-inline {{
    width: 120px; height: 4px; background: rgba(255,255,255,0.06);
    border-radius: 2px; overflow: hidden;
  }}
  .pos-fill-inline {{
    height: 100%; border-radius: 2px;
    background: linear-gradient(90deg, #c8ff00, #7b3fe4);
  }}

  /* Two-column layout */
  .main-grid {{ display: grid; grid-template-columns: 3fr 2fr; gap: 16px; margin-bottom: 20px; }}

  /* Panel */
  .panel {{
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px; padding: 20px;
  }}
  .panel-title {{
    font-size: 13px; font-weight: 600; color: #aaa;
    text-transform: uppercase; letter-spacing: 0.8px;
    margin-bottom: 16px; display: flex; align-items: center; gap: 8px;
  }}
  .panel-title::before {{
    content: ''; display: block; width: 3px; height: 14px;
    background: #7b3fe4; border-radius: 2px;
  }}

  /* Grid visualization */
  .grid-viz {{ width: 100%; }}
  .grid-viz svg {{ width: 100%; height: auto; }}

  /* Badge */
  .badge {{
    display: inline-flex; align-items: center; padding: 3px 10px;
    border-radius: 20px; font-size: 11px; font-weight: 600;
    letter-spacing: 0.3px;
  }}
  .badge-bullish {{ background: rgba(200,255,0,0.1); color: #c8ff00; }}
  .badge-bearish {{ background: rgba(255,76,139,0.12); color: #ff4c8b; }}
  .badge-neutral {{ background: rgba(136,136,136,0.12); color: #888; }}
  .badge-ranging {{ background: rgba(123,63,228,0.12); color: #7b3fe4; }}

  /* MTF row */
  .mtf-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .mtf-item {{
    display: flex; align-items: center; gap: 8px;
    background: rgba(255,255,255,0.02); border-radius: 8px;
    padding: 8px 12px; border: 1px solid rgba(255,255,255,0.04);
    font-size: 12px; cursor: help; position: relative;
  }}
  .mtf-item[title]:hover::after {{
    content: attr(title); position: absolute;
    bottom: calc(100% + 6px); left: 0; z-index: 10;
    background: #1a1a1a; color: #ccc; border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px; padding: 8px 12px; font-size: 11px; font-weight: 400;
    white-space: normal; width: 240px; line-height: 1.5;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  }}
  .mtf-label {{ font-size: 12px; color: #aaa; }}
  .mtf-value {{ margin-left: auto; font-weight: 500; font-size: 11px; color: #ccc; }}

  /* Events timeline */
  .events {{ max-height: 400px; overflow-y: auto; }}
  .events::-webkit-scrollbar {{ width: 4px; }}
  .events::-webkit-scrollbar-thumb {{ background: #333; border-radius: 2px; }}
  .event-row {{
    display: flex; align-items: flex-start; gap: 12px;
    padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.03);
    font-size: 12px;
  }}
  .event-time {{ color: #555; white-space: nowrap; font-family: monospace; font-size: 11px; min-width: 42px; }}
  .event-tag {{
    font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 3px;
    white-space: nowrap; flex-shrink: 0;
  }}
  .event-text {{ color: #aaa; line-height: 1.4; word-break: break-all; }}

  /* Risk controls */
  .risk-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .risk-item {{
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; border-radius: 8px;
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.04);
    font-size: 12px; cursor: help;
    position: relative;
  }}
  .risk-item[title]:hover::after {{
    content: attr(title); position: absolute;
    bottom: calc(100% + 6px); left: 0; z-index: 10;
    background: #1a1a1a; color: #ccc; border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px; padding: 8px 12px; font-size: 11px; font-weight: 400;
    white-space: normal; width: 240px; line-height: 1.5;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  }}
  .risk-icon {{ font-size: 14px; }}
  .risk-name {{ color: #aaa; }}
  .risk-status {{ margin-left: auto; font-weight: 500; font-size: 11px; }}
  .risk-ok {{ color: #c8ff00; }}
  .risk-warn {{ color: #f0a030; }}
  .risk-alert {{ color: #ff4c8b; }}

  .full-width {{ grid-column: 1 / -1; }}

  /* Position bar */
  .position-bar {{
    width: 100%; height: 6px; background: rgba(255,255,255,0.06);
    border-radius: 3px; margin-top: 8px; overflow: hidden;
  }}
  .position-fill {{
    height: 100%; border-radius: 3px;
    background: linear-gradient(90deg, #c8ff00, #7b3fe4);
  }}

  /* Footer */
  .footer {{
    text-align: center; padding: 24px 0; margin-top: 16px;
    border-top: 1px solid rgba(255,255,255,0.04);
    font-size: 11px; color: #444;
  }}
  /* LP Dashboard */
  .lp-portfolio {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 0;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px; margin-bottom: 16px; overflow: hidden;
  }}
  .lp-port-col {{ padding: 24px 32px; }}
  .lp-port-right {{ border-left: 1px solid rgba(255,255,255,0.06); }}
  .lp-section-label {{
    font-size: 10px; color: #555; text-transform: uppercase;
    letter-spacing: 0.8px; margin-bottom: 8px;
  }}
  .lp-section-title {{
    font-size: 20px; font-weight: 700; margin: 24px 0 6px;
    letter-spacing: -0.3px;
  }}
  .lp-big-num {{
    font-size: 32px; font-weight: 700; letter-spacing: -1px;
    margin-bottom: 16px;
  }}
  .lp-apr {{
    font-size: 14px; font-weight: 500; color: #888; margin-left: 8px;
  }}
  .lp-detail-rows {{ display: flex; flex-direction: column; gap: 8px; }}
  .lp-row {{
    display: flex; justify-content: space-between; align-items: center;
    font-size: 13px;
  }}
  .lp-row-label {{ color: #888; }}
  .lp-row-val {{ font-weight: 500; }}
  .lp-sub {{ padding-left: 16px; }}
  .lp-sub .lp-row-label {{ color: #555; font-size: 12px; }}
  .lp-sub .lp-row-val {{ font-size: 12px; }}
  .lp-strat-card {{
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px; padding: 16px 24px; margin: 16px 0;
  }}
  .lp-strat-header {{
    display: flex; justify-content: space-between; align-items: center;
  }}
  .lp-status-pill {{
    font-size: 11px; font-weight: 600; padding: 2px 10px;
    border: 1px solid; border-radius: 20px;
  }}
  .lp-info-row {{
    display: grid; grid-template-columns: 1fr 1fr 3fr; gap: 12px;
    margin-bottom: 12px;
  }}
  .lp-token-row {{
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
    margin-bottom: 12px;
  }}
  .lp-info-card {{
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px; padding: 16px 20px;
  }}
  .lp-info-val {{
    font-size: 18px; font-weight: 600; margin-top: 4px;
  }}
  .lp-range-card {{
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px; padding: 16px 20px;
  }}
  .lp-range-hdr {{
    display: flex; justify-content: space-between; align-items: center;
    font-size: 12px; margin-bottom: 12px;
  }}
  .lp-range-track {{
    height: 24px; background: rgba(255,255,255,0.04);
    border-radius: 6px; position: relative; margin-bottom: 8px;
  }}
  .lp-range-fill {{
    position: absolute; top: 2px; bottom: 2px;
    background: rgba(200,255,0,0.12);
    border: 1px solid rgba(200,255,0,0.25);
    border-radius: 4px;
  }}
  .lp-price-dot {{
    position: absolute; top: 50%; transform: translate(-50%, -50%);
    width: 3px; height: 16px; background: #c8ff00;
    border-radius: 2px; box-shadow: 0 0 8px rgba(200,255,0,0.5);
  }}
  .lp-range-labels {{
    display: flex; justify-content: space-between;
    font-size: 11px; color: #555;
  }}

  {tab_css}
  /* Responsive */
  @media (max-width: 768px) {{
    .portfolio-strip {{ flex-direction: column; }}
    .port-item {{ border-right: none; border-bottom: 1px solid rgba(255,255,255,0.06); width: 100%; }}
    .position-inline {{ margin-left: 0; width: 100%; }}
    .main-grid {{ grid-template-columns: 1fr; }}
    .risk-grid {{ grid-template-columns: 1fr; }}
    .mtf-grid {{ grid-template-columns: 1fr; }}
    .tab-bar {{ flex-wrap: wrap; }}
    .lp-portfolio {{ grid-template-columns: 1fr; }}
    .lp-port-right {{ border-left: none; border-top: 1px solid rgba(255,255,255,0.06); }}
    .lp-info-row {{ grid-template-columns: 1fr 1fr; }}
    .lp-range-card {{ grid-column: 1 / -1; }}
    .lp-token-row {{ grid-template-columns: 1fr 1fr; }}
  }}
</style>
</head>
<body>
<div class="bg-aurora"></div>
<div class="container">

  {tab_buttons}

  {"".join(sections)}

  <div class="footer">
    策略看板 · {now_str}
  </div>
</div>
{tab_js}
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Multi-strategy trading dashboard")
    parser.add_argument(
        "--config",
        help="Path to dashboard_config.json (multi-strategy mode)",
    )
    # Legacy single-strategy args (backward compatible)
    parser.add_argument(
        "--state",
        help="Path to grid_state_v4.json (legacy single-strategy mode)",
    )
    parser.add_argument(
        "--log",
        default="/tmp/grid_bot_cron.log",
        help="Path to cron log (legacy mode)",
    )
    parser.add_argument(
        "--out",
        default=str(SCRIPT_DIR / "dashboard.html"),
        help="Output HTML path",
    )
    args = parser.parse_args()

    if args.config:
        # Config-driven multi-strategy mode
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        config = json.loads(config_path.read_text())
        strategies = config.get("strategies", [])
        # Propagate global settings as defaults into each strategy config
        global_keys = ("chart_interval", "chart_hours", "okx_api_url")
        for strat in strategies:
            for key in global_keys:
                if key not in strat and key in config:
                    strat[key] = config[key]
    elif args.state:
        # Legacy single-strategy mode
        strategies = [
            {
                "id": "eth-grid",
                "name": "ETH 网格交易",
                "type": "grid",
                "state_file": args.state,
                "log_file": args.log,
            }
        ]
    else:
        # Default: look for config, fallback to state file in same dir
        config_path = SCRIPT_DIR / "dashboard_config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
            strategies = config.get("strategies", [])
            # Propagate global settings as defaults into each strategy config
            global_keys = ("chart_interval", "chart_hours", "okx_api_url")
            for strat in strategies:
                for key in global_keys:
                    if key not in strat and key in config:
                        strat[key] = config[key]
        else:
            # Fallback: assume grid state in same directory
            strategies = [
                {
                    "id": "eth-grid",
                    "name": "ETH 网格交易",
                    "type": "grid",
                    "state_file": str(SCRIPT_DIR / "grid_state_v4.json"),
                    "log_file": "/tmp/grid_bot_cron.log",
                }
            ]

    html = generate_dashboard(strategies)
    Path(args.out).write_text(html)
    print(f"Dashboard written to {args.out}")


if __name__ == "__main__":
    main()
