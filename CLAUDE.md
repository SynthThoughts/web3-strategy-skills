# AI Agent — 链上交易策略项目

OKX OnchainOS 链上交易策略的开发、回测、部署、可视化全栈项目。

## 项目结构

```
AI/Agent/
├── okx-strategy-factory/    # Agent Team 工厂（元技能）
│   ├── SKILL.md             #   5 步流水线定义 + 状态机
│   ├── roles/               #   5 个 Agent 角色（lead/strategy/backtest/infra/publish/iteration）
│   ├── references/          #   接口规范 + 策略经验库
│   ├── hooks/               #   质量门禁 + 空闲重分配
│   └── assets/              #   Skill 模板 + 发布脚本
│
├── Strategy/                # 策略工作空间（工厂产出）
│   └── {strategy}/          #   每个策略独立目录
│       ├── Script/v{ver}/   #     代码 + config + risk-profile
│       ├── Backtest/v{ver}/ #     回测报告 + 资金曲线
│       └── Iteration/       #     迭代复盘
│
├── grid-trading/            # 已发布: ETH 网格交易 Skill
├── cl-lp-rebalancer/        # 已发布: V3 LP 自动调仓 Skill
├── polymarket-arb-scanner/  # Polymarket 套利扫描
│
├── dashboard/               # 策略可视化看板（纯 Python → 自包含 HTML）
│   ├── generate_dashboard.py  # 多策略渲染器（grid + cl_lp）
│   ├── dashboard_config.json  # 策略数据源配置
│   └── demo-dashboard/        # V3 LP 看板原型（React，仅参考）
│
└── Agentic Wallet/          # OKX onchainos CLI（独立项目，核心依赖）
    ├── onchainos             #   本地二进制 (arm64, v2.1.0)
    ├── cli/                  #   Rust 源码
    └── skills/               #   12 个 onchainos Skill 定义
```

## 核心依赖关系

```
onchainos CLI (Agentic Wallet/)
    ↑ 运行时调用
策略脚本 (grid/cl-lp/...)
    ↑ 生成 + 回测
okx-strategy-factory (Agent Team)
    ↑ 读取 state
dashboard (可视化)
```

- **onchainos**: 独立项目，更新频繁，独立测试。能力已作为 Claude Code Skill 安装，按 skill 名调用即可
- **策略工厂**: 协调 5 个 Agent（Strategy/Backtest/Infra/Publish/Iteration），不写代码只协调
- **dashboard**: 零依赖 Python，VPS 上 cron 生成静态 HTML

## 部署拓扑

| 环境 | 用途 | onchainos |
|------|------|-----------|
| 本地 Mac | 开发 + 回测 | `Agentic Wallet/onchainos` (arm64) |
| VPS (见 1Password "OpenClaw") | 实盘 + 看板 | `/usr/local/bin/onchainos` (amd64) |

VPS 通过 SSH 部署，部署方式见各策略 Skill 内置说明。SSH 配置见 `~/.ssh/config`。

## 已有策略

| 策略 | 状态 | 链 | 特点 |
|------|------|-----|------|
| grid-trading | LIVE | Base | 动态网格 + MTF 趋势 + 非对称步距 |
| cl-lp-rebalancer | 待部署 | Base | V3 LP 波动率自适应范围 + 趋势不对称 |

## 策略变更验证

策略代码修改后，必须按顺序通过以下验证才算 done：

1. **代码验证** — lint + format + type check（按语言：Python 用 ruff/pyright，Rust 用 cargo check）
2. **逻辑验证** — 核心交易逻辑的单元测试通过，边界条件覆盖（如价格为 0、余额不足、API 超时）
3. **交易验证** — 用测试账户（Account 3）dry-run 或小额实盘验证下单/撤单/仓位计算正确
4. **数据统计验证** — 回测指标（胜率、最大回撤、夏普比率）与改动前对比，无意外恶化

## 同步发布

策略变更验证通过后，必须同时同步以下四处，不能只更新部分：

1. **本地仓库** — git commit
2. **VPS 部署** — SSH 更新脚本 + 重启服务
3. **GitHub** — git push（账号: SynthThoughts）
4. **ClawHub** — `npx clawhub publish <dir> --version <semver> --changelog "<msg>"`

通过 subagent 执行（模板见全局 CLAUDE.md Context 保护）。

## 敏感信息保护

以下内容**禁止**提交到 Git / GitHub / ClawHub：

- `.env` 文件、私钥、API Key、Secret、Passphrase
- 1Password 条目引用（`op://`）
- 钱包地址与 Account ID 的映射关系
- VPS IP、SSH 密钥路径

使用 `.gitignore` 排除，发布前检查 `git diff --cached` 确认无泄漏。

