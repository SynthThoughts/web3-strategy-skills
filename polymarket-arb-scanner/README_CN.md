# Polymarket 套利扫描器

Polymarket CLOB（中央限价订单簿）三层套利检测框架。

## 特性

- **第 1a 层：单条件套利** — 单个市场的 YES + NO 定价偏差
- **第 1b 层：负风险多结果套利** — 多结果事件中 ∑YES 定价偏差
- **第 2 层：跨市场隐含套利** — 相关市场间的逻辑依赖关系违反
- **假阳性过滤** — overround 检查、冷门市场检测、已结算市场过滤、方向陷阱
- **基于 CLOB 定价** — 始终使用真实订单簿数据，不用 Gamma API 的不准确价格
- **深度感知利润计算** — 考虑每个价格层级的实际流动性

## 架构

```
Gamma API (元数据)  →  MarketStore (过滤活跃、高成交量市场)
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        第 1a 层           第 1b 层          第 2 层
      单条件套利        负风险事件套利      跨市场套利
      YES/NO 偏差        ∑YES 偏差       隐含关系违反
              │                │                │
              ▼                ▼                ▼
           CLOB API ←──────────────────────→ CLOB API
          (订单簿)                          (订单簿)
              │                │                │
              ▼                ▼                ▼
        质量过滤器        质量过滤器       假阳性过滤器
              │                │                │
              └────────────────┼────────────────┘
                               ▼
                      按 maxProfit 排序输出
```

## 安装

**ClawHub**（推荐）:
```bash
npx clawhub install polymarket-arb-scanner
```

**手动安装**:
```bash
cp -r polymarket-arb-scanner ~/.openclaw/skills/
```

## 目录结构

```
polymarket-arb-scanner/
└── SKILL.md    # 核心知识：套利层级、CLOB 获取、过滤器、模式匹配
```

## 前置条件

- Python 3.10+ 或 Node.js 18+
- 能访问 Polymarket API（Gamma + CLOB）
- 无需 API Key（公开端点）

## 许可证

Apache-2.0
