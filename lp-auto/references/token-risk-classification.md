# Token Risk Classification Spec

定义了 LP 池风险评估所用的**代币分类**与**pair 风险档位**。

这是 source-of-truth；`token_registry.py` 是它的 Python 实现，两者必须保持一致。新增/调整代币请**同时改两处**。

---

## 1 · 代币分类（4 类 + other）

### 1.1 `bluechip`
顶级 L1 资产 + 1:1 pure wrap。

| Symbol | 说明 |
|---|---|
| `ETH`, `WETH`, `cbETH` | ETH 与其无漂移 wrap |
| `BTC`, `WBTC`, `cbBTC`, `tBTC` | BTC 与其 wrap |

**准入标准**：
- 市值 top-5 且 10 年内无 > 50% 日跌幅（除系统崩盘）
- Wrap 必须是 1:1 pegged 的 smart contract（`cbETH` 虽然累计 staking yield，年漂移 <1%，算 1:1）
- LST（stETH/weETH 等）**不在此列**，见 § 1.2

### 1.2 `lst` (Liquid Staking Tokens)
追踪 ETH 价值但**有额外协议风险**的质押代币。

| Symbol | 发行方 |
|---|---|
| `stETH`, `wstETH` | Lido |
| `weETH`, `eETH` | EtherFi |
| `rETH` | Rocket Pool |
| `osETH` | Stakewise |
| `ETHx` | Stader |
| `swETH` | Swell |
| `ankrETH` | Ankr |

**为什么独立一档**：
- 虽然价格跟随 ETH，但有 slashing / smart contract / governance 风险
- 历史案例：**stETH 在 2022 年 6 月 Celsius/3AC 崩盘时 depeg 到 0.94**
- 把 LST 归 bluechip 会低估风险

### 1.3 `stable`
USD-pegged 稳定币。

| Symbol | 类型 |
|---|---|
| `USDC`, `USDT`, `DAI` | 主流 |
| `USDG`, `USDS`, `FRAX` | 新兴 |
| `sDAI`, `crvUSD`, `LUSD` | yield-bearing / CDP |
| `GUSD`, `PYUSD`, `TUSD`, `FDUSD` | 合规发行 |
| `MIM`, `USDe`, `sUSDe` | 合成/delta-neutral |

**准入标准**：
- 1:1 USD-pegged（或跟随一篮子 USD 类指数）
- TVL > $50M 且 12 个月内无 >10% 持续 depeg

### 1.4 `native`
L1/L2 的 gas / governance 代币（**`ETH` / `BTC` 不算**，它们归 bluechip）。

| Symbol | Chain |
|---|---|
| `OP` | Optimism |
| `ARB` | Arbitrum |
| `BNB` | BSC |
| `MATIC`, `POL` | Polygon |
| `AVAX` | Avalanche |
| `SOL` | Solana |
| `S` | Sonic |
| `SUI`, `APT`, `TRX`, `TON` | 各自 L1 |
| `NEAR`, `FTM`, `CRO`, `CELO` | 同上 |

**准入标准**：该 token 是某条独立 L1/L2 的 native gas/treasury 代币。

**DeFi 蓝筹（`LINK`、`UNI`、`AAVE`、`CRV` 等）不在此列** —— 归 `other`。理由：它们不是链基础设施，波动率和解锁风险与真正的 L1 gas 不同。

### 1.5 `other`
所有未被 1.1–1.4 覆盖的代币，包括：
- DeFi 蓝筹（`LINK`, `UNI`, `AAVE`, `CRV`, `MKR`, `SNX`, ...）
- Meme 币（`PEPE`, `WIF`, `DEGEN`, `VIRTUAL`, ...)
- 新币 / 长尾 / 未索引

**策略上默认当 high-risk 处理**（fail-safe）。

---

## 2 · 风险档位（6 档，升序）

| Tier | 典型 Pair | 说明 | IL 上限 |
|---|---|---|---|
| `very-low` | `USDC-USDT`, `ETH-WETH`, `WBTC-cbBTC` | 稳定币对 / pure wrap | ~0 |
| `low` | `ETH-stETH`, `WETH-weETH` | Bluechip-ETH × LST 同家族 | <1% |
| `medium` | `ETH-USDC`, `BTC-USDC`, `stETH-USDC` | Bluechip × 稳定币 | 10–30% |
| `medium-high` | `ETH-BTC`, `ETH-WBTC`, `BTC-stETH` | 两个不同的 bluechip 资产 | 5–15% |
| `high` | `OP-USDC`, `ARB-ETH`, `BNB-USDC` | Native × 稳定币/bluechip | 20–50% |
| `very-high` | `PEPE-USDC`, `OP-ARB`, `ETH-meme` | 含 other 或双 native | 不稳定 |

### 2.1 判定规则（Python `risk_tier()` 实现逻辑）

按优先级从上往下匹配，第一个命中即返回：

1. **same pure-wrap group**（同组 ETH/WETH/cbETH 或 BTC/WBTC/cbBTC/tBTC） → `very-low`
2. **stable × stable** → `very-low`
3. **bluechip × LST (ETH 家族内)** → `low`
4. **{bluechip, stable}** 或 **{lst, stable}** → `medium`
5. **bluechip × bluechip**（不同资产） → `medium-high`
6. **{lst, lst}** 或 **bluechip × LST（跨家族，罕见）** → `medium-high`
7. **native ∈ pair** 且 **(stable | bluechip) ∈ pair** → `high`
8. **兜底**（含 other / 双 native） → `very-high`

---

## 3 · Normalization（Symbol 归一化）

Python `normalize()` 处理：
- 统一大写：`usdc` → `USDC`
- 去桥接/链后缀：`USDC.e` → `USDC`、`WETH.BASE` → `WETH`、`ARB.arb` → `ARB`

**未来改进**：用 `tokenAddress + chainId` 作唯一 ID 替代 symbol 字符串匹配。当前 symbol 匹配已够用，但多链环境下（比如同链上两个不同的 "USDT"）会有歧义。

---

## 4 · 使用策略

| 角色 | 默认风险上限 |
|---|---|
| 保守资金 | `very-low`（只做稳定币 + pure-wrap） |
| 稳健策略 | `medium`（含 ETH/USDC 类） |
| 增长策略 | `medium-high` |
| 主动管理 | `high`（需要人工盯盘 + stop loss） |
| **禁用默认** | `very-high` 需显式参数开启 |

Python 入口：`allowed(tier, max_tier)` 返回 `True` iff `tier` 不高于 `max_tier`。

---

## 5 · 维护

**新增 token 流程**：

1. 判定属于哪一类（bluechip/lst/stable/native/other）
2. 在本 MD 对应的 § 1.x 表格补一行
3. 在 `token_registry.py` 的对应 Python set 里加 symbol
4. 跑 self-test：`python3 token_registry.py`
5. 如果该 token 的分类边界有争议，补一条 self-test case

**不确定时归 `other`** —— 安全默认。

**分类争议常见**：
- 新 LST 应算 `lst` 还是 `bluechip`？→ 默认 `lst`
- 某 DeFi 蓝筹应算 `native`？→ 不，归 `other`
- WETH 和 stETH 应在一个 wrap group 里？→ 不，stETH 归 `lst`（有协议风险）

---

## 6 · 为什么这样分？（设计理由）

### 6.1 分 4 档 + other，不是 3 档
LST 和 bluechip 拆开、native 和 bluechip 拆开，是两个对 risk-unaware 分类的**必要修正**。前者防 Lido 黑天鹅，后者防把 `OP-USDC` 误当 `ETH-USDC`。

### 6.2 6 档 tier 而不是 3 档
实际 LP 风险光谱宽，3 档（低/中/高）把 `USDC-USDT` 和 `ETH-USDC` 混在同一"低"档，决策时无法区分。6 档给足判别能力。

### 6.3 `other` = `very-high`
策略里不认识的代币都当高风险，避免意外卷入 meme 币 / rug。要开 `other` 必须用户显式改 `max_tier=very-high`。

### 6.4 LST-only pair（`stETH-weETH`）被归 `medium-high`
两个 LST 都有各自协议风险，叠加不等于 0。保守归到 `medium-high`。
