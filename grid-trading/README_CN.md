# 网格交易

[English](./README.md)

基于 OKX DEX API 的 EVM L2 链上动态网格交易策略，适用于任意交易对。

## 特性

- **非对称网格间距** — 看涨时买侧密集/卖侧稀疏，看跌时反转
- **多时间框架趋势分析** — 5 分钟价格历史 + 1 小时 K 线 ATR
- **趋势自适应仓位管理** — 等额、马丁格尔、反马丁格尔、金字塔模式
- **卖出拖尾优化** — 强上涨趋势中延迟卖出
- **HODL Alpha 追踪** — 对比策略收益与简单持有
- **全面风控** — 止损、止盈、回撤保护、熔断机制
- **Discord 通知** — 交易提醒、每日报告

## 架构

```
Cron (5分钟) → Python 脚本 → onchainos CLI → OKX Web3 API → 链上
                   ↓                ↓
             state_v1.json    钱包 (TEE 签名)
                   ↓
             MTF 分析 → 趋势自适应网格 → Discord
```

## 安装

**ClawHub**（推荐）:
```bash
npx clawhub install grid-trading
```

**OpenClaw + cron**:
```bash
cp -r grid-trading ~/.openclaw/skills/
cp grid-trading/references/eth_grid_v1.py ~/.openclaw/scripts/

openclaw cron add --name eth-grid-tick \
  --schedule "*/5 * * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v1.py tick"

openclaw cron add --name eth-grid-daily \
  --schedule "0 0 * * *" \
  --command "cd ~/.openclaw/scripts && python3 eth_grid_v1.py report"
```

**系统 crontab**:
```bash
scp grid-trading/references/eth_grid_v1.py user@your-vps:~/scripts/

crontab -e
# */5 * * * * cd ~/scripts && python3 eth_grid_v1.py tick >> /tmp/grid.log 2>&1
# 0 0 * * *   cd ~/scripts && python3 eth_grid_v1.py report >> /tmp/grid.log 2>&1
```

## 目录结构

```
grid-trading/
├── SKILL.md              # 核心知识：算法、流水线、配置
└── references/
    ├── eth_grid_v1.py     # 生产策略脚本
    └── grid-algorithm.md  # 算法详解：网格数学、MTF、非对称
```

## 前置条件

- onchainos CLI — `npx skills add okx/onchainos-skills`
- OKX API Key，需有 DEX 交易权限
- OnchainOS Agentic Wallet，需启用 TEE 签名
- Python 3.10+
- VPS（推荐，用于 7×24 运行）

## 许可证

Apache-2.0
