# Grid Trading Daily Report Template

Used by the `report` sub-command to generate daily performance reports (Chinese).

---

## Template Format

```
ETH 网格交易日报 ({date})

📊 市场概况
  ETH 价格: ${price} (24h {change_pct}%)
  趋势: {trend} (强度 {strength})
  结构: {structure}
  1h 动量: {momentum_1h}%
  4h 动量: {momentum_4h}%
  ATR 波动率: {kline_atr_pct}%

💰 组合概况
  ETH: {eth_balance} ({eth_pct}%)
  USDC: ${usdc_balance} ({usdc_pct}%)
  总价值: ${total_usd}
  成本基础: ${cost_basis}

📈 收益表现
  总 PnL: ${total_pnl} ({pnl_pct}%)
  网格利润: ${grid_profit}
  HODL Alpha: ${hodl_alpha}
  峰值回撤: {drawdown_pct}%

🔄 交易统计 (24h)
  交易次数: {trades_24h} (买 {buys_24h} / 卖 {sells_24h})
  成功率: {success_rate}%
  平均利差: {avg_spread}%

🔄 累计统计
  总交易: {total_trades} (买 {total_buys} / 卖 {total_sells})
  运行时间: {running_days} 天
  日均交易: {avg_daily_trades}

📐 当前网格
  中心: ${grid_center}
  步长: ${grid_step} ({step_pct}%)
  范围: ${grid_low} - ${grid_high}
  类型: {grid_type}
  当前级别: {current_level}/{grid_levels}

⚠️ 风控状态
  止损: {stop_loss_status}
  追踪止损: {trailing_stop_status}
  连续错误: {consecutive_errors}
  信号: {signal_status}
```

---

## Field Descriptions

| Field | Source | Calculation |
|-------|--------|-------------|
| `price` | Latest swap quote | — |
| `change_pct` | price_history | `(price - price_288_ago) / price_288_ago * 100` |
| `trend` | MTF analysis | bullish / bearish / neutral |
| `strength` | MTF analysis | 0.0 - 1.0 |
| `structure` | MTF analysis | uptrend / downtrend / ranging |
| `total_pnl` | PnL tracking | `total_usd - cost_basis` |
| `pnl_pct` | PnL tracking | `total_pnl / cost_basis * 100` |
| `grid_profit` | `stats.grid_profit` | Sum of realized round-trip profits |
| `hodl_alpha` | HODL tracking | `total_usd - (initial_eth_amount * price)` |
| `drawdown_pct` | Trailing stop | `(peak - current) / peak * 100` |
| `avg_spread` | Trade pairs | Average `(sell_price - buy_price) / buy_price` |

---

## Discord Embed Format

Daily reports are sent as Discord embeds with:
- Color: Gold (#FFD700)
- Title: "ETH Grid Daily Report - {date}"
- Footer: "Grid Trading v4 | Running {days}d"

---

## Usage

```bash
# Generate and send daily report
python eth_grid_v4.py report

# Typical cron setup (08:00 CST = 00:00 UTC)
0 0 * * * cd /path/to/script && python eth_grid_v4.py report
```
