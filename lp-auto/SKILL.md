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

# 一键装调度（检测平台 + 装 cron/launchd/schtasks/... + 登记 + 自检）
lp-auto install [--scheduler TYPE] [--interval 300]
  → 默认: Linux→cron, macOS→launchd, Windows→Task Scheduler
  → 其他选项: systemd-user / daemon-foreground / manual

# 跑一次 tick（前台 one-shot；调试用；不做调度）
lp-auto start

# Tier 1 跨平台守护进程（Linux/Mac/Windows 通用）
lp-auto daemon --interval 300
  → 前台 while-loop；由外部进程管理器（systemd/launchd/Task Scheduler/tmux/nohup）
    负责保活。SIGTERM/SIGINT 优雅退出

# 向 status 登记"谁在调度我"（平台原生调度器装完必调）
lp-auto scheduler-register --type systemd-user --id lp-auto@prod.service --interval 300

# 查看当前状态 + 调度器自检（读 scheduler.json → 按类型分派检查）
lp-auto status
  → {pool, token_id, range, LP value, PnL, time_in_range, selector_last_check,
     scheduler.alive, last_tick_age}

# 手动触发换池评估（不执行）
lp-auto select

# 手动执行换池（使用 selector 推荐的目标）
lp-auto switch

# 完全退出（close 当前仓位；调度器需由用户/AI 自行停）
lp-auto stop
```

## 调度（Scheduling） — 持续后台运行

Skill 把"让 `tick` 循环跑起来"拆成三档，按平台选：

### 决策树

```
1. 探测平台: uname -s (Linux/Darwin) 或 ver (Windows)
2. 有平台原生 service manager 且用户允许装？
   ├─ Linux + systemd (rootless 优先)  → Tier 2a systemd --user
   ├─ macOS                             → Tier 2b launchd
   ├─ Windows                           → Tier 2c Task Scheduler
   └─ 都没有 / 用户拒绝
      ├─ 想要轻量不怕重启丢？          → Tier 1 `lp-auto daemon` + tmux/nohup
      └─ 纯 Linux/Mac + 已习惯 cron？  → Tier 3 crontab
3. 装完**强制**调 `lp-auto scheduler-register`，否则 status 失去自检锚点
4. 调 `lp-auto status` 校验：scheduler alive + last_tick_age 在容忍范围内
```

### 平台模板

位于 `references/scheduler/`，全部是示例，AI 需按用户环境替换占位符：
`{{PYTHON}} {{CLI}} {{INSTANCE}} {{HOME}} {{USER}} {{INTERVAL}}`。

| Tier | 平台 | 模板 | scheduler-register type |
|---|---|---|---|
| 1 | all | `nohup-fallback.sh.example` | `daemon-foreground` |
| 2a | Linux | `systemd-user.service.example` | `systemd-user` |
| 2b | macOS | `launchd.plist.example` | `launchd` |
| 2c | Windows | `windows-task.xml.example` | `windows-task` |
| 3 | Linux/Mac | `cron.example` | `cron` |

### AI 启动 playbook

**首选：一条命令**
```bash
lp-auto install --interval 300
```
`install` 自动走完：平台探测 → 渲染模板 → 装调度器 → 写 scheduler.json → 自检。

**手动（当 install 不适用，例如要定制 unit 文件）**：
```
Step 1. uname -s → 决定走哪档
Step 2. 读对应模板 → 替换占位符 → 写到平台正确位置
Step 3. 启用 + 立即启动（systemctl enable --now / launchctl load / schtasks /Run / tmux new）
Step 4. 必须调 lp-auto scheduler-register --type X --id Y --interval N
Step 5. 必须调 lp-auto status 确认 "✓ type=X" + last_tick_age 正常
Step 6. 若 status 显示 ✗ 或 ⚠ → 回滚（禁用/删除 unit）+ 报告用户
```

Step 4 是强约束：scheduler.json 是 status 做自检的唯一锚点，忘了这步等于回到"不知道谁在调度"的黑箱状态。

### 停止调度

`lp-auto stop` 只关仓位，不碰调度器。AI 停调度需按安装档对称操作：
- systemd-user → `systemctl --user disable --now <unit>` + `rm` unit 文件
- launchd → `launchctl unload <plist>` + `rm` plist
- windows-task → `schtasks /Delete /TN <name> /F`
- cron → 从 `crontab -e` 删行
- daemon-foreground → `kill $(cat <instance>/daemon.pid)`

然后删 `scheduler.json`（或调 `scheduler-register --type manual` 标记为手动）。

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

实例目录 `~/.lp-auto/instances/<name>/` 结构：

| 文件 | 写入方 | 用途 |
|---|---|---|
| `config.json` | `init` / `switch` | 策略参数 + pool_config |
| `state.json` | `cl_lp.py` 每 tick | position + stats + `_cached_snapshot` |
| `pool_selector_state.json` | `select` | streak + 推荐 |
| `scheduler.json` | `scheduler-register` | 记录调度器类型/id，供 status 自检 |
| `daemon.pid` | `daemon` | Tier 1 PID 文件 |
| `daemon.log` / `cron.log` / `tick.log` | 调度器 | stdout/stderr |

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
│   ├── install.sh                    # 本地/VPS 安装脚本
│   └── scheduler/                    # 跨平台调度器模板（供 AI 读取改写）
│       ├── README.md
│       ├── systemd-user.service.example
│       ├── launchd.plist.example
│       ├── windows-task.xml.example
│       ├── cron.example
│       └── nohup-fallback.sh.example
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
