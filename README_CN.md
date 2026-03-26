# Web3 策略技能库

[English](./README.md)

面向 AI 编程智能体的 Web3 交易技能集合。每个技能是一个独立的 `SKILL.md`，教会 AI 智能体如何构建、部署和运行特定的交易策略。

## 技能一览

| 技能 | 版本 | 运行环境 | 说明 |
|------|------|----------|------|
| [okx-strategy-factory](./okx-strategy-factory/) | v1.0.0 | 本地 (Claude Code / Cursor / Gemini CLI / Codex) | 元技能：协调 5 个 AI 智能体，完成 OKX OnchainOS 交易策略的开发、回测、部署、发布和迭代全流程。 |
| [grid-trading](./grid-trading/) | v1.0.0 | 服务器 (OpenClaw / VPS cron) | EVM L2 链上动态网格交易。多时间框架分析、趋势自适应仓位、非对称网格步长。 |
| [cl-lp-rebalancer](./cl-lp-rebalancer/) | v1.0.0 | 服务器 (OpenClaw / VPS cron) | DEX 集中流动性 LP 区间再平衡器。 |
| [cross-funding-arb](./cross-funding-arb/) | v1.0.0 | 服务器 (OpenClaw / VPS cron) | 跨交易所永续合约资金费率套利。Hyperliquid + Binance Delta 中性对冲。 |
| [polymarket-arb-scanner](./polymarket-arb-scanner/) | v1.0.0 | 服务器 (OpenClaw / VPS cron) | Polymarket CLOB 三层套利检测：单条件、负风险多结果、跨市场隐含关系。 |

## 整体架构

```
┌─────────────────────────────────────────────────────────┐
│  本地: 你的 IDE / 终端                                    │
│                                                          │
│  okx-strategy-factory (元技能)                            │
│  ├── Strategy Agent   → 编写交易逻辑                      │
│  ├── Backtest Agent   → 历史数据验证                      │
│  ├── Publish Agent    → 打包为独立技能                     │
│  ├── Infra Agent      → 部署到服务器 ──────────────┐      │
│  └── Iteration Agent  → 复盘与优化            ◄────┤      │
│                                                    │      │
└────────────────────────────────────────────────────┤──────┘
                                                     │
┌────────────────────────────────────────────────────▼──────┐
│  服务器: VPS / OpenClaw                                    │
│                                                            │
│  grid-trading           (每 5 分钟 cron → tick → 交易)     │
│  cl-lp-rebalancer       (每 5 分钟 cron → 调仓)            │
│  cross-funding-arb      (每 5 分钟 cron → 套利)            │
│  polymarket-arb-scanner (cron → 扫描 → 告警)               │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

## 安装

### 1. 策略工厂（本地开发）

安装到你的 **本地 IDE**，让 AI 智能体帮你开发和管理策略：

**ClawHub**（推荐 OpenClaw 用户使用）:
```bash
npx clawhub install okx-strategy-factory
```

**Claude Code**:
```bash
# 项目级
cp -r okx-strategy-factory /path/to/project/.claude/skills/

# 全局（所有项目可用）
cp -r okx-strategy-factory ~/.claude/skills/
```

**Cursor**:
```bash
cp -r okx-strategy-factory /path/to/project/.cursor/skills/
```

**Gemini CLI**:
```bash
cp -r okx-strategy-factory /path/to/project/.gemini/skills/
```

安装后，对你的 AI 智能体说：
```
使用 okx-strategy-factory 技能，为 Base 链上的 ETH/USDC 开发一个网格交易策略。
```

### 2. 交易策略（服务器部署）

安装到 **服务器 / VPS** 上，7×24 小时运行策略：

**ClawHub**（推荐）:
```bash
npx clawhub install grid-trading
npx clawhub install cl-lp-rebalancer
npx clawhub install cross-funding-arb
npx clawhub install polymarket-arb-scanner
```

**OpenClaw + cron**（以 grid-trading 为例）:
```bash
# 安装技能
cp -r grid-trading ~/.openclaw/skills/

# 部署策略脚本
cp grid-trading/references/eth_grid_v1.py ~/.openclaw/scripts/

# 注册定时任务
openclaw cron add --name eth-grid-tick \
  --schedule "*/5 * * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v1.py tick"

openclaw cron add --name eth-grid-daily \
  --schedule "0 0 * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v1.py report"
```

**系统 crontab**（不使用 OpenClaw）:
```bash
# 将脚本复制到服务器
scp grid-trading/references/eth_grid_v1.py user@your-vps:~/scripts/

# 编辑 crontab
crontab -e
# */5 * * * * cd ~/scripts && python3 eth_grid_v1.py tick >> /tmp/grid.log 2>&1
# 0 0 * * *   cd ~/scripts && python3 eth_grid_v1.py report >> /tmp/grid.log 2>&1
```

**一键安装**（自动检测平台）:
```bash
cd grid-trading
./install.sh                          # 自动检测
./install.sh --platform openclaw      # OpenClaw + 注册定时任务
./install.sh --platform claude        # Claude Code
```

### 3. 仅获取知识（适用于任何 AI 智能体）

每个 `SKILL.md` 都是纯 Markdown，可以直接粘贴到任何 AI 智能体的系统提示词中：

```bash
cat grid-trading/SKILL.md | pbcopy   # macOS 复制到剪贴板
```

## 前置条件

| 依赖 | 用途 | 安装方式 |
|------|------|----------|
| onchainos CLI | grid-trading, cl-lp-rebalancer, strategy-factory | `npx skills add okx/onchainos-skills` |
| OKX API Key | grid-trading, cl-lp-rebalancer | 通过 1Password 或环境变量 |
| OnchainOS 钱包 | grid-trading, cl-lp-rebalancer | `onchainos wallet login` |
| Hyperliquid 私钥 | cross-funding-arb | EIP-712 签名密钥或 Agent Wallet |
| Binance Futures API Key | cross-funding-arb | 需开通 USDT-M 合约权限 |
| Python 3.10+ | 策略脚本 | 系统包管理器 |
| VPS（可选） | 7×24 交易 | 任意 Linux 服务器 |
| 1Password CLI（可选） | 安全凭证管理 | `brew install 1password-cli` |

## 技能目录结构

```
skill-name/
├── SKILL.md          # 核心知识（必需）
├── references/       # 详细文档：CLI 参考、算法、风控（可选）
├── roles/            # 智能体角色定义（可选，strategy-factory 使用）
├── assets/           # 模板和资源（可选）
├── hooks/            # 任务门禁脚本（可选）
├── install.sh        # 多平台安装器（可选）
└── README.md         # 用户安装和使用指南（可选）
```

## 贡献

欢迎 PR。添加新技能的步骤：

1. 创建以策略命名的文件夹
2. 编写 `SKILL.md` — 不仅教 AI "怎么做"，更要说明"为什么"
3. 添加 `references/` 存放详细文档，`install.sh` 方便安装
4. 包含你遇到过的反模式和失败场景

## 许可证

Apache-2.0
