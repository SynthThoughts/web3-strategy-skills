---
name: lp-auto
description: 通用 V3 集中流动性 LP 自动化策略。按风险等级自动选池 → ATR 自适应 tick 范围 → 出区间自动调仓 → 同风险档位内自动迁移到更优池。支持任何 onchainos 兼容链上的 V3 池（WETH-USDC、USDC-cbBTC、稳定币对、LST 配对等）。Agent 调用 `lp-auto init/start/status/switch/stop` 即可完成全流程，无需理解 V3 内部机制。
---

# lp-auto — Universal V3 LP Strategy Skill

自动化的 Uniswap V3 集中流动性管理：**发现池 → 评分 → 开仓 → 调仓 → 换池**，一条 pipeline 贯穿。

## 核心能力

1. **按风险选池**：`--risk medium` 只在 `bluechip × stable` 档位里搜索（分类见 `references/token-risk-classification.md`）
2. **ATR 自适应 range**：基于实时 ATR × regime multiplier 计算 tick 范围，低波动窄、高波动宽
3. **自动调仓**：OOR 时自动 close → ratio probe → minimal swap → deposit（全 onchainos 路径）
4. **自动换池**：每小时扫同风险档其他池，若某候选 **连续 2 次** 净收益高出当前 >30%（扣切换成本后），自动迁移
5. **风险护栏**：gas reserve、trailing stop、max drawdown、position age cooldown、max rebalances/24h

## 安装前置

- `onchainos` CLI 已登录（`onchainos wallet status` 返回 `loggedIn: true`）
- Python 3.10+
- 目标链上钱包持有 native token（ETH）≥ `gas_reserve_eth`（默认 0.02 ETH）+ 本金

## 命令

```bash
# 初始化新实例（自动发现当前风险档位下的最优池，生成 state）
lp-auto init --chain base --risk medium --capital 500
  → 扫描 → 选中 ETH-USDC 0.3% → 打印将执行的建仓方案

# 启动自动循环（cron 或 systemd 触发 tick 每 5 分钟）
lp-auto start

# 查看当前状态
lp-auto status
  → {pool, token_id, range, LP value, PnL, time_in_range, selector_last_check}

# 手动触发换池评估（不执行)
lp-auto select-pool

# 手动执行换池（使用 selector 推荐的目标）
lp-auto switch

# 完全退出（close 当前仓位 + 停止 cron/systemd）
lp-auto stop
```

## 参数（`references/config.default.json`）

| key | 默认 | 说明 |
|---|---|---|
| `chain` | `base` | 链名称（onchainos 支持：`ethereum` `base` `arbitrum` `optimism` `polygon` `bsc` ...） |
| `max_risk` | `medium` | 风险上限：`very-low` `low` `medium` `medium-high` `high` `very-high` |
| `capital_usd` | `500` | 投入本金（不含 gas reserve） |
| `gas_reserve_eth` | `0.02` | 永久保留的 ETH 数量（用于支付 gas） |
| `slippage_pct` | `0.1` | swap 最大滑点容忍（0.1%） |
| `range_mult` | `{low:0.4, medium:0.8, high:1.2, extreme:1.5}` | ATR × multiplier = half_width_pct |
| `min_range_pct` | `0.5` | 半宽下限（%）；fee_tier 决定绝对下限（tick_spacing） |
| `max_range_pct` | `30` | 半宽上限（%） |
| `trailing_stop_pct` | `0.10` | Trailing stop 阈值（peak 回撤 10% 止损） |
| `stop_loss_pct` | `0.15` | 绝对止损 15% |
| `max_rebalances_24h` | `6` | 24h 最大调仓次数 |
| `min_position_age_seconds` | `3600` | 最小持仓时间 1h 才允许 rebalance |
| `auto_switch` | `false` | 是否自动换池（保守起见默认关闭） |
| `main_max_leftover_usd` | `50` | no-swap 配比后余额超此值触发 minimal swap |

注：`tick_spacing` 由 `fee_tier` 自动派生（V3 factory 映射：0.01%→1, 0.05%→10, 0.3%→60, 1%→200），无需手动配置。

## 调仓流程

```
OOR detected
  → close_position (onchainos defi redeem)
  → get_balances (native ETH + WETH + USDC)
  → calc_optimal_range (ATR × regime mult, trend asymmetry)
  → ratio probe (onchainos defi deposit dry-run 探测 ETH:USDC 比例)
  → no-swap balanced deposit?
      ✓ → deposit (onchainos defi deposit)
      ✗ leftover > $50 → minimal swap (仅差额) → deposit
  → pre-deposit WETH unwrap (如 native ETH 不够, unwrap WETH + poll OKX indexer)
  → verify token_id + LP value on-chain
  → peak reset (清零, 让下一 tick 自然建立)
```

所有链上操作通过 `onchainos` CLI（defi deposit/redeem + wallet contract-call），私钥在 onchainos 内部。

## 多实例

每个实例独立 state，通过 `LP_AUTO_INSTANCE_DIR` 环境变量或 `--instance <name>` 参数区分：

```bash
LP_AUTO_INSTANCE=arb_conservative lp-auto init --chain arbitrum --risk low --capital 300
LP_AUTO_INSTANCE=base_growth      lp-auto init --chain base --risk medium-high --capital 1000
```

State 文件位于 `~/.lp-auto/instances/<name>/state.json`。

## Skill 文件结构

```
lp-auto/
├── SKILL.md                          # 本文
├── references/
│   ├── cli.py                        # CLI 入口（init/start/status/switch/stop）
│   ├── cl_lp.py                      # 主引擎（tick loop + rebalance + risk checks）
│   ├── capital_efficiency.py         # CE optimizer (range picking)
│   ├── pool_config.py                # 任意 V3 池参数化
│   ├── pool_compare.py               # 风险过滤的池子发现
│   ├── pool_selector.py              # 同档内 CE 排序 + streak 记录
│   ├── pool_switch.py                # 跨池切换执行
│   ├── token_registry.py             # 代币 4 类 + 6 档风险
│   ├── token-risk-classification.md  # 风险分级 spec
│   ├── range-algorithm.md            # Range 选择 + CE scoring 算法
│   ├── ce_execute.py                 # Close+reopen 单池 rebalance
│   ├── ce_add_liquidity.py           # 补仓到现有 NFT
│   ├── backtest_ce.py                # CE 回测工具
│   ├── config.default.json           # 默认参数
│   └── install.sh                    # VPS 安装脚本
└── tests/
    ├── test_capital_efficiency.py
    ├── test_cleanup_residual.py
    ├── test_pool_config.py
    └── test_token_registry.py
```

## 下一步（未实现）

- Cross-chain bridging（换到其他链的池）
- LP NFT 组合：多池同时持仓
- Impermanent loss hedge via options / perps
