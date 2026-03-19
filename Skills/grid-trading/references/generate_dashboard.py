#!/usr/bin/env python3
"""Generate a dark-themed HTML dashboard from grid bot state and logs.

Usage:
    python3 generate_dashboard.py [--state grid_state_v4.json] [--log /tmp/grid_bot_cron.log] [--out dashboard.html]

Reads the bot's state file and cron log, produces a self-contained HTML dashboard
styled after the OKX Onchain OS design language.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TARGET_POINTS = 288  # 24h at 5min intervals


# ── Data extraction ────────────────────────────────────────────────────────


def fetch_kline_prices(limit: int = TARGET_POINTS) -> list[float]:
    """Fetch 5min close prices from OKX public API (no auth needed).

    Returns oldest-first list of close prices, or empty list on failure.
    """
    url = (
        "https://www.okx.com/api/v5/market/candles"
        f"?instId=ETH-USDT&bar=5m&limit={limit}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "grid-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        candles = data.get("data", [])
        # OKX returns newest-first: [ts, o, h, l, c, vol, ...]
        prices = [float(c[4]) for c in reversed(candles)]
        return prices
    except Exception:
        return []


def ensure_24h_prices(state_prices: list[float]) -> list[float]:
    """Ensure we have ~24h of price data by backfilling from OKX API if needed."""
    if len(state_prices) >= TARGET_POINTS:
        return state_prices[-TARGET_POINTS:]

    gap = TARGET_POINTS - len(state_prices)
    kline_prices = fetch_kline_prices(TARGET_POINTS)
    if not kline_prices:
        return state_prices  # API failed, use what we have

    if len(state_prices) == 0:
        return kline_prices[-TARGET_POINTS:]

    # Backfill: take older kline prices to fill the gap, then append state prices
    # State prices are more accurate (actual tick prices), so they take priority
    backfill = kline_prices[:gap] if len(kline_prices) > gap else kline_prices
    combined = backfill + state_prices
    return combined[-TARGET_POINTS:]


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


# ── HTML generation ────────────────────────────────────────────────────────


def generate_html(state: dict, json_blocks: list[dict], events: list[dict]) -> str:
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
    signal = state.get("signal_cache", {})
    bullish_score = signal.get("bullish_score", 0)

    # Status
    status = latest.get("status", "unknown")
    stop_triggered = state.get("stop_triggered")
    version = latest.get("version", "4.1")

    # Trades summary
    total_trades = stats.get("total_trades", 0)
    buy_count = stats.get("buy_successes", 0)
    sell_count = stats.get("sell_successes", 0)

    # Price history for chart — ensure 24h by backfilling from OKX API
    spark_prices = ensure_24h_prices(price_history)

    # Combined grid + price chart SVG
    chart_svg = _build_chart_svg(
        spark_prices, level_prices, current_price, current_level, grid_range, trades
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

    # Compute strategy decision variables (must match calc_dynamic_grid directional logic)
    vol_mult_val = 1.5
    if strength > 0.3:
        if trend == "bullish":
            vol_mult_val = 1.5 + (3.0 - 1.5) * strength  # 1.5 → 3.0
        elif trend == "bearish":
            vol_mult_val = 1.5 - (1.5 - 1.0) * strength  # 1.5 → 1.0

    # Position sizing multipliers
    if trend == "bullish":
        buy_mult = 1.0 + strength * 0.5  # up to 1.5x
        sell_mult = 1.0 - strength * 0.3  # down to 0.7x
    elif trend == "bearish":
        buy_mult = 1.0 - strength * 0.3
        sell_mult = 1.0 + strength * 0.5
    else:
        buy_mult = 1.0
        sell_mult = 1.0

    # Position limits (must match _get_position_limits in eth_grid_v4.py)
    if trend == "bullish" and strength > 0.3:
        pos_max = 70 + int((80 - 70) * strength)
        pos_min = 30
    elif trend == "bearish" and strength > 0.3:
        pos_max = 70
        pos_min = 30 - int((30 - 25) * strength)
    else:
        pos_max = 70
        pos_min = 30

    # Pre-compute tooltip strings (can't use % inside f-strings)
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
    # Pre-compute asymmetric step display strings
    if is_asymmetric:
        step_tip = (
            f"非对称网格: 买入步长 ${buy_step_val:.1f} / 卖出步长 ${sell_step_val:.1f}。"
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
        step_value_html = f"${step:,.1f} ({step / current_price * 100:.1f}%)"
        step_footer = f"步长 ${step:,.1f} ({step / current_price * 100:.1f}%)"

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

    status_color = "#c8ff00"
    status_label = "运行中"
    if stop_triggered:
        status_color = "#ff4c8b"
        status_label = "已停止"
    elif status == "no_trade":
        status_color = "#7b3fe4"
        status_label = "监控中"

    pnl_sign = "+" if pnl >= 0 else ""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>网格交易看板</title>
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

  /* Sparkline */
  .sparkline {{ width: 100%; margin-top: 12px; }}
  .sparkline svg {{ width: 100%; height: auto; }}

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

  /* MTF row — same sizing as risk items */
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

  /* Full-width panel */
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

  /* Responsive */
  @media (max-width: 768px) {{
    .portfolio-strip {{ flex-direction: column; }}
    .port-item {{ border-right: none; border-bottom: 1px solid rgba(255,255,255,0.06); width: 100%; }}
    .position-inline {{ margin-left: 0; width: 100%; }}
    .main-grid {{ grid-template-columns: 1fr; }}
    .risk-grid {{ grid-template-columns: 1fr; }}
    .mtf-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<div class="bg-aurora"></div>
<div class="container">

  <!-- Header -->
  <div class="header">
    <div class="header-left">
      <div class="logo">网格 <span>交易</span></div>
      <span class="version">v{version} · ETH/USDC · Base</span>
    </div>
    <div style="display:flex;align-items:center;gap:16px;">
      <div class="status-badge">
        <div class="status-dot" style="background:{status_color};box-shadow:0 0 8px {status_color};"></div>
        <span style="color:{status_color};">{status_label}</span>
      </div>
      <span class="timestamp">{now_str}</span>
    </div>
  </div>

  <!-- 1. Portfolio Strip -->
  <div class="portfolio-strip">
    <div class="port-item primary">
      <span class="port-label">总资产</span>
      <span class="port-val lg">${total_usd:,.2f}</span>
    </div>
    <div class="port-item">
      <span class="port-label">盈亏</span>
      <span class="port-val {"lime" if pnl >= 0 else "pink"}">{pnl_sign}${abs(pnl):,.2f}</span>
    </div>
    <div class="port-item">
      <span class="port-label">网格利润</span>
      <span class="port-val">${grid_profit:,.2f}</span>
    </div>
    <div class="port-item">
      <span class="port-label">交易</span>
      <span class="port-val">{total_trades} <span style="font-size:11px;color:#555;font-weight:400;">{buy_count}买 {sell_count}卖</span></span>
    </div>
    <div class="position-inline">
      <span style="font-size:11px;color:#555;">ETH {eth_pct:.0f}%</span>
      <div class="pos-bar-inline"><div class="pos-fill-inline" style="width:{eth_pct:.1f}%;"></div></div>
      <span style="font-size:11px;color:#555;">USDC {100 - eth_pct:.0f}%</span>
    </div>
  </div>

  <!-- 2. Grid + Price (full width) -->
  <div class="panel" style="margin-bottom:20px;">
    <div class="grid-viz">{chart_svg}</div>
    <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#aaa;">
      <div style="display:flex;gap:16px;">
        <span title="网格中心价格，基于 20H EMA">中心 <span style="color:#ccc;font-weight:600;">${grid_center:,.0f}</span></span>
        <span title="当前价格所在层级 / 总层数">层级 <span style="color:#ccc;font-weight:600;">L{current_level}/{grid.get("levels", 6)}</span></span>
        <span title="{grid_pos_tip}" style="color:{"#c8ff00" if current_level < grid.get("levels", 6) / 2 else "#ff4c8b" if current_level > grid.get("levels", 6) / 2 else "#aaa"};">{grid_pos_label}</span>
        <span title="市场情绪评分 (0-1)：基于 Smart Money 整体活跃度，> 0.3 时放大买入">信号 <span style="color:{"#c8ff00" if bullish_score > 0.3 else "#555"};font-weight:600;">{bullish_score:.2f}</span></span>
      </div>
      <span style="color:#555;">{step_footer} · ATR {atr_pct:.2f}%</span>
    </div>
  </div>

  <!-- 3. Analysis + Risk (left) | Log (right) -->
  <div class="main-grid">
    <div class="panel">
      <div class="panel-title">策略决策</div>
      <!-- Trend badge row with strength bar -->
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
        <span class="badge badge-{trend}" title="EMA(25m) ${ema_short:,.0f} / EMA(1h) ${ema_medium:,.0f} / EMA(4h) ${ema_long:,.0f}。{"短>中>长 看涨排列" if ema_short > ema_medium > ema_long else "短<中<长 看跌排列" if ema_short < ema_medium < ema_long else "交叉无序"}。影响仓位倍数和宽度倍数">{trend_cn}</span>
        <span class="badge badge-{structure if structure != "ranging" else "ranging"}" title="8H 窗口分 4 段，高低点严格递增=上升、递减=下降、否则=震荡。影响卖出延迟策略">{structure_cn}</span>
        <div style="flex:1;display:flex;align-items:center;gap:6px;" title="趋势强度 = |EMA短-EMA长| / EMA长 / 2% 归一化。> 30% 时触发宽度倍数上调和仓位倍数偏移">
          <div style="flex:1;height:4px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden;">
            <div style="width:{strength * 100:.0f}%;height:100%;background:{"#c8ff00" if trend == "bullish" else "#ff4c8b" if trend == "bearish" else "#555"};border-radius:2px;"></div>
          </div>
          <span style="font-size:11px;color:#aaa;font-weight:600;">{strength:.0%}</span>
        </div>
      </div>
      <!-- Decision variables grid -->
      <div class="mtf-grid">
        <div class="mtf-item" title="{step_tip}">
          <span class="mtf-label">网格步长</span>
          <span class="mtf-value">{step_value_html}</span>
        </div>
        <div class="mtf-item" title="1H K线真实波幅 (ATR)，步长计算的核心输入。ATR 越高网格越宽">
          <span class="mtf-label">波动率</span>
          <span class="mtf-value">{atr_pct:.2f}% ATR</span>
        </div>
        <div class="mtf-item" title="{vol_mult_tip}">
          <span class="mtf-label">宽度倍数</span>
          <span class="mtf-value" style="color:{"#c8ff00" if vol_mult_val > 1.5 else "#ff4c8b" if vol_mult_val < 1.5 else "#ccc"};">{vol_mult_val:.1f}x</span>
        </div>
        <div class="mtf-item" title="{sizing_tip}">
          <span class="mtf-label">仓位倍数</span>
          <span class="mtf-value"><span style="color:#c8ff00;">买{buy_mult:.2f}</span> <span style="color:#ff4c8b;">卖{sell_mult:.2f}</span></span>
        </div>
        <div class="mtf-item" title="ETH 持仓上限（超过阻止买入）和下限（低于阻止卖出），趋势强度 > 30% 时偏移">
          <span class="mtf-label">仓位限制</span>
          <span class="mtf-value">{pos_min}% – {pos_max}%</span>
        </div>
        <div class="mtf-item" title="1H 动量 > 0.5% 且看涨时阻止卖出；30min 跌幅 > 2% 时阻止买入">
          <span class="mtf-label">动量</span>
          <span class="mtf-value" style="color:{"#c8ff00" if momentum_1h > 0 else "#ff4c8b" if momentum_1h < 0 else "#ccc"};">{"+" if momentum_1h > 0 else ""}{momentum_1h:.2f}% 1H</span>
        </div>
      </div>
      <div style="margin-top:20px;">
        <div class="panel-title">风控状态</div>
        {risk_html}
      </div>
    </div>
    <div class="panel">
      <div class="panel-title">活动日志</div>
      <div class="events">{log_html}</div>
    </div>
  </div>

  <div class="footer">
    网格交易 v{version} · {now_str}
  </div>
</div>
</body>
</html>"""


def _build_chart_svg(
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
        # price_history spans n*5 minutes ending "now"
        from datetime import datetime, timezone

        now_ts = datetime.now(timezone.utc).timestamp()
        span_seconds = (n - 1) * 5 * 60  # total timespan in seconds
        start_ts = now_ts - span_seconds

        for t in trades:
            try:
                t_time = t.get("time", "")
                t_dir = t.get("direction", "")
                t_price = float(t.get("price", 0))
                if not t_time or not t_price:
                    continue

                # Parse ISO timestamp
                if t_time.endswith("Z"):
                    t_time = t_time[:-1] + "+00:00"
                trade_dt = datetime.fromisoformat(t_time)
                if trade_dt.tzinfo is None:
                    trade_dt = trade_dt.replace(tzinfo=timezone.utc)
                trade_ts = trade_dt.timestamp()

                # Skip trades outside the chart timespan
                if trade_ts < start_ts or trade_ts > now_ts:
                    continue

                # Map to x/y
                frac = (trade_ts - start_ts) / span_seconds if span_seconds > 0 else 1
                tx = pad_x + frac * inner_w
                ty = y_for(t_price)

                if t_dir == "BUY":
                    # Upward triangle (lime green)
                    elements.append(
                        f'<polygon points="{tx:.1f},{ty - 8:.1f} '
                        f'{tx - 5:.1f},{ty + 2:.1f} {tx + 5:.1f},{ty + 2:.1f}" '
                        f'fill="#c8ff00" fill-opacity="0.9"/>'
                    )
                elif t_dir == "SELL":
                    # Downward triangle (pink)
                    elements.append(
                        f'<polygon points="{tx:.1f},{ty + 8:.1f} '
                        f'{tx - 5:.1f},{ty - 2:.1f} {tx + 5:.1f},{ty - 2:.1f}" '
                        f'fill="#ff4c8b" fill-opacity="0.9"/>'
                    )

                # Price label next to marker
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

    # Normalize all entries into a common format: (sort_key, time_short, tag, color, detail)
    entries: list[tuple[str, str, str, str, str]] = []

    # Events
    for ev in events:
        ts = ev["time"]
        time_short = ts.split(" ")[-1][:5] if " " in ts else ts[:5]
        detail = ev["detail"]
        detail = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*", "", detail)
        etype = ev["type"]
        color = type_colors.get(etype, "#555")
        tag = tag_labels.get(etype, etype)
        entries.append((ts, time_short, tag, color, detail))

    # Trades
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

    # Sort by timestamp descending
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


def _build_risk_controls_html(state: dict, mtf: dict) -> str:
    stats = state.get("stats", {})
    price_history = state.get("price_history", [])
    # (icon, name, value, css_class, tooltip)
    items: list[tuple[str, str, str, str, str]] = []

    # ── Portfolio-level (abort tick) ──

    # 1. Stop-loss: portfolio drops 15% below cost basis
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

    # 2. Trailing stop: portfolio drops 10% from peak
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
            f"从峰值回撤超 10% 触发追踪止损。"
            f"峰值 ${peak:,.2f}，当前 ${total_usd:,.2f}，距触发 {10 - drawdown_pct:.1f}%",
        )
    )

    # 3. Circuit breaker
    consec_err = stats.get("consecutive_errors", 0)
    if consec_err >= 5:
        items.append(
            (
                "🔌",
                "熔断器",
                f"已触发 ({consec_err})",
                "risk-alert",
                "连续 5 次错误暂停交易 1h。成功 tick 重置。当前已熔断",
            )
        )
    else:
        items.append(
            (
                "🔌",
                "熔断器",
                f"{consec_err}/5",
                "risk-ok",
                f"连续 5 次错误暂停交易 1h。当前 {consec_err} 次，距触发 {5 - consec_err} 次",
            )
        )

    # ── Trade-level (per-tick checks) ──

    # 4. Cooldown: 30min same-direction
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

    # 6. Rapid drop protection (flash crash guard)
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
            "急跌保护",
            f"{drop_pct:.1f}%" if drop_pct > 0.1 else "正常",
            "risk-warn" if drop_active else "risk-ok",
            f"30 分钟内跌幅 > 2% 时阻止买入（防闪崩接刀）。"
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
                "卖出保护",
                f"生效 {momentum:+.2f}%",
                "risk-warn",
                f"看涨 + 1H 动量 {momentum:+.2f}% > 0.5%，阻止卖出。"
                "趋势转弱或动量回落后自动解除",
            )
        )
    else:
        items.append(
            (
                "🛡",
                "卖出保护",
                "待命",
                "risk-ok",
                f"看涨趋势下 1H 动量 > 0.5% 时阻止卖出。"
                f"当前趋势 {trend}，动量 {momentum:+.2f}%",
            )
        )

    # 8. Position limits check
    eth_balance = state.get("last_balances", {}).get("eth", 0)
    usdc_balance = state.get("last_balances", {}).get("usdc", 0)
    cur_price = price_history[-1] if price_history else 0
    eth_val = eth_balance * cur_price
    eth_pct_now = (
        eth_val / (eth_val + usdc_balance) * 100 if (eth_val + usdc_balance) > 0 else 0
    )
    # Recalculate limits from mtf (match _get_position_limits)
    strength = mtf.get("strength", 0)
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


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Generate grid trading dashboard")
    parser.add_argument(
        "--state",
        default=str(SCRIPT_DIR / "grid_state_v4.json"),
        help="Path to grid_state_v4.json",
    )
    parser.add_argument(
        "--log", default="/tmp/grid_bot_cron.log", help="Path to cron log"
    )
    parser.add_argument(
        "--out", default=str(SCRIPT_DIR / "dashboard.html"), help="Output HTML path"
    )
    args = parser.parse_args()

    state = load_state(Path(args.state))
    json_blocks = parse_log_jsons(Path(args.log))
    events = parse_log_events(Path(args.log))

    html = generate_html(state, json_blocks, events)
    Path(args.out).write_text(html)
    print(f"Dashboard written to {args.out}")


if __name__ == "__main__":
    main()
