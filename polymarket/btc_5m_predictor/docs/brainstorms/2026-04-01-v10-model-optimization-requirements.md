---
date: 2026-04-01
topic: v10-model-optimization
---

# v10 模型三线并行优化

## Problem Frame

当前冠军模型 v10_30d_d3_20260331 使用 5 个特征在 30 天数据上取得了 CV AUC 0.6615 / Sharpe 11.98 的基线表现。但存在明显的提升空间：

- 仅 5 个特征，对 taker_vol 高度依赖，信号来源单一
- 特征间潜在的交互关系未被显式利用
- 模型结构固定为 CatBoost depth=3，未探索其他配置和集成方式
- CV-HO gap 0.037，泛化能力仍有改善余地

需要从特征选择、特征交互、模型优化三个方向系统搜索，找到可复现的提升。

## Baseline (v10)

| 指标 | 值 |
|------|-----|
| 特征数 | 5 (taker_vol_raw, price_vs_rvwap_60, cvd_slope_10, hour4_sin, vpt_sum_30) |
| 数据范围 | 30 天 (6634 样本) |
| CV AUC | 0.6615 |
| HO AUC | 0.6242 |
| CV-HO gap | 0.037 |
| Sharpe | 11.98 |
| Win Rate | 65.1% |
| Max DD | -$120 |
| 模型 | CatBoost, depth=3, Depthwise |

## Requirements

**特征选择优化**

- R1. 在 v10 的 5 特征基础上，系统评估增加 3-10 个补充特征的效果（目标覆盖更多信号维度：跨交易所、链上、情绪等）
- R2. 每个候选特征必须通过单变量 AUC 筛选 + forward selection 验证，避免噪声特征稀释信号
- R3. 评估不同特征数量档位（5/8/10/15）对 AUC 和 Sharpe 的影响曲线，找到最优特征数

**特征交互优化**

- R4. 基于 v10 的 5 个核心特征构建交互特征：乘积、比率、差值、条件组合等
- R5. 重点探索 taker_vol 与其他 4 个特征的交叉信号（如 taker_vol × cvd_slope、taker_vol / vpt_sum 等）
- R6. 评估非线性交互（分位数交叉、regime-conditional features）是否优于简单算术交互

**模型优化**

- R7. 探索 CatBoost 自身的优化空间：depth 4-6 在更多特征下是否合理、loss function 对比（Logloss vs CrossEntropy）、更大 Optuna 搜索空间
- R8. 评估轻量集成方案：2-3 个不同随机种子/特征子集的 CatBoost 模型取平均（限训练时评估，部署仍用单模型）
- R9. 探索概率校准对 Sharpe 的影响（Platt scaling / isotonic regression）

**实验管理**

- R10. 所有实验使用相同的 30 天数据窗口和 4-fold purged CV，确保可比性
- R11. 每个实验记录 run_id 到 model_runs 表，附带 tags 标记所属优化方向
- R12. 三线并行执行，最终选取综合最优的配置作为 v11 候选

## Success Criteria

- CV AUC ≥ 0.6815（相对 v10 提升 +0.02）
- 回测 Sharpe ≥ 13.98（相对 v10 提升 +2）
- 以上两个条件同时满足才认定为显著提升
- CV-HO gap 不恶化（≤ 0.04）
- 至少一个优化方向产出有效提升

## Scope Boundaries

- 数据范围保持 30 天不变，不引入数据量这个变量
- 不更换模型框架（不引入 LightGBM/XGBoost/神经网络），聚焦 CatBoost 生态
- 不修改标签定义（±0.03% 阈值不变）
- 不修改回测逻辑；特征工程代码（features.py）可扩展以支持新特征和交互特征，但回测和评估流程不变
- 每个方向 5-8 个实验，总计 15-24 个实验

## Key Decisions

- **保持 30 天数据窗口**: 先验证优化方向有效性，避免同时改变数据量和模型
- **三线并行**: 三个方向独立实验，最后交叉验证最优组合
- **成功门槛 AUC +0.02 且 Sharpe +2**: 要求两个指标同时提升，避免过拟合单一指标
- **CatBoost only**: 避免引入新框架的复杂度，在已知有效的框架内深挖

## Dependencies / Assumptions

- 本地 DuckDB 数据已同步，包含完整的 30 天 futures 数据
- 训练 pipeline (`btc train`) 支持 `--features-include/exclude` 和 `--tags` 参数
- v10 的 5 个特征在 `data/features.py` 中均已实现
- 实验对比通过 `btc experiment compare` 进行
- ho_auc 当前训练时计算但未持久化到 model_runs 表，需补充存储以支持 CV-HO gap 对比

## Outstanding Questions

### Deferred to Planning

- [Affects R1][Needs research] 当前 feature_metadata.py 中有哪些高质量候选特征可直接使用？需要新建哪些？
- [Affects R4][Technical] CatBoost 是否原生支持特征交互声明，还是需要手动构建交互列？
- [Affects R6][Needs research] regime-conditional features 如何实现？按波动率分位还是趋势状态？
- [Affects R8][Technical] 多种子集成在当前 train_pipeline 中如何实现？需要修改 pipeline 还是在外层循环？
- [Affects R9][Needs research] CatBoost 输出的概率是否已经足够校准，Platt scaling 是否有边际收益？

## Next Steps

→ `/ce:plan` for structured implementation planning
