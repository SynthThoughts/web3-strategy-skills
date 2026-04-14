---
name: lp-auto
description: 通用 V3 集中流动性 LP 自动化策略。按风险等级自动选池 → 资本效率最优化挑选 tick 范围 → 出区间自动调节 → 同风险档位内自动迁移到更优池。支持任何 onchainos 兼容链上的 V3 池（WETH-USDC、USDC-cbBTC、稳定币对、LST 配对等）。用户场景：一键 LP 部署、按风险偏好投 DeFi、多池组合管理、V3 调仓、手续费最大化、无常损失管理。Agent 调用 `lp-auto init/start/status/switch/stop` 即可完成全流程，无需理解 V3 内部机制。
---

# lp-auto — Universal V3 LP Strategy Skill

自动化的 Uniswap V3 集中流动性管理：**发现池 → 评分 → 开仓 → 调仓 → 换池**，一条 pipeline 贯穿。

## 核心能力

1. **按风险选池**：`--risk medium` 只在 `bluechip × stable` 档位里搜索（分类见 `references/token-risk-classification.md`）
2. **资本效率最优化 range**：基于 on-chain **depth + price history + hourly APY** 三路数据扫 25 个候选 range，取 `net = fee − rebalance_cost` 最大的（算法：`references/range-algorithm.md`）
3. **自动调仓**：OOR / ATR drift / volatility regime 漂移时自动 rebalance
4. **自动换池**：每小时扫同风险档其他池，若某候选 **连续 2 次** 净收益高出当前 >30%（扣切换成本后），自动迁移
5. **风险护栏**：0.02 ETH gas reserve、trailing stop、max drawdown、per-pool cooldown

## 安装前置

- `onchainos` CLI 已登录（`onchainos wallet status` 返回 `loggedIn: true`）
- Python 3.10+
- 目标链上钱包至少 0.05 ETH（gas buffer）和 $300+ 本金

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
| `auto_switch` | `false` | 是否自动换池（保守起见默认关闭，手动开 opt-in） |
| `switch_uplift_threshold` | `0.30` | 换池 uplift 门槛（30%） |
| `switch_streak_required` | `2` | 连续多少次 selector 运行都推荐同一目标才触发 |
| `rebalance_cost_usd` | `5.0` | 单次 rebalance 成本估计（gas + 2% swap slippage） |
| `min_apy_gate` | `0.10` | APY 低于此值拒绝进场 |
| `trailing_stop_pct` | `0.10` | Trailing stop 阈值 |

## 多实例

每个实例独立 state，通过 `LP_AUTO_INSTANCE` 环境变量或 `--instance <name>` 参数区分：

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
│   ├── cl_lp.py                      # 主引擎（tick loop + rebalance）
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
│   ├── config.default.json           # 默认参数
│   └── install.sh                    # VPS 安装脚本
```

## 与 cl-lp-rebalancer (v1) 的关系

- `cl-lp-rebalancer` = 单池版本（WETH-USDC Base 0.3% 硬编码）
- `lp-auto` = v2，参数化支持任意池 + 自动选池 + 自动换池

### 从 v1 迁移到 lp-auto (实战步骤，2026-04-15 生产验证过)

1. **复制 state 文件**：
   ```bash
   mkdir -p ~/.lp-auto/instances/prod
   cp ~/scripts/cl-lp/cl_lp_state.json ~/.lp-auto/instances/prod/state.json
   ```

2. **生成 config.json**（从旧 flat config 映射到 nested `pool_config`）：
   ```python
   old = json.load(open("~/scripts/cl-lp/config.json"))
   new = {
     "chain": old["pool_chain"],
     "capital_usd": old["initial_investment_usd"],
     "pool_config": {
       "investment_id": old["investment_id"],
       "chain": old["pool_chain"],
       "token0_symbol": old["token0"]["symbol"],
       "token0_address": old["token0"]["address"],
       "token0_decimals": old["token0"]["decimals"],
       "token1_symbol": old["token1"]["symbol"],
       "token1_address": old["token1"]["address"],
       "token1_decimals": old["token1"]["decimals"],
       "fee_tier": old["fee_tier"],
       "tick_spacing": old["tick_spacing"],
     },
     "auto_switch": False, "auto_edges": False,
     "dynamic_width": {"enabled": False},
   }
   ```

3. **替换 scheduler cron**（VPS 用 zeroclaw daemon，不是 crontab）：
   ```bash
   zeroclaw --config-dir ~/.zeroclaw-strategy cron remove <old_uuid>
   zeroclaw --config-dir ~/.zeroclaw-strategy cron add --tz "Asia/Shanghai" \
     "*/5 * * * *" "cd ~/scripts/cl-lp && set -a && . ./.env && set +a && \
     LP_AUTO_INSTANCE_DIR=~/.lp-auto/instances/prod python3 cl_lp.py tick"
   ```

4. **更新 nginx alias**（dashboard 数据源）：
   ```nginx
   location = /lp/state.json {
       alias /home/ubuntu/.lp-auto/instances/prod/state.json;
   }
   ```
   然后 `sudo nginx -t && sudo systemctl reload nginx`。

5. **验证**：`lp-auto --instance prod status` + dashboard 刷新看 LP 数据同步。

### 保留 v1 作 backup

旧 `~/scripts/cl-lp/cl_lp_state.json` 迁移后**不要删** — 万一 lp-auto 出问题可以 rollback：回滚步骤是反向执行 step 3+4。

## 下一步（未实现，见 roadmap）

- Cross-chain bridging（换到其他链的池）
- LP NFT 组合：多池同时持仓
- Impermanent loss hedge via options / perps
