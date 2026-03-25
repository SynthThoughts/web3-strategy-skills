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

VPS 通过 SSH 部署，pm2 管理进程，OpenClaw 推送 Discord 通知。

### VPS SSH 连接

SSH 走本地 Stash 代理（HTTP `127.0.0.1:7890`）以保证稳定性，已配置在 `~/.ssh/config`：

```
Host 43.133.182.170
  ProxyCommand /usr/bin/nc -X connect -x 127.0.0.1:7890 %h %p
  ServerAliveInterval 10
  ServerAliveCountMax 3
  ControlMaster auto
  ControlPath ~/.ssh/sockets/%r@%h-%p
  ControlPersist 600
```

- 需要 Stash 保持运行，否则 SSH 连接会失败
- ControlMaster 复用连接，避免频繁新建连接触发 VPS 的 MaxStartups 限制

## 已有策略

| 策略 | 状态 | 链 | 特点 |
|------|------|-----|------|
| grid-trading | LIVE | Base | 动态网格 + MTF 趋势 + 非对称步距 |
| cl-lp-rebalancer | 待部署 | Base | V3 LP 波动率自适应范围 + 趋势不对称 |

## 凭证管理

通过 `op` (1Password CLI) 获取，禁止明文。onchainos 通过 `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` 环境变量认证。
