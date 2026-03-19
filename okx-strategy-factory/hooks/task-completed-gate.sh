#!/usr/bin/env bash
# TaskCompleted hook — 质量门禁
# Exit 0: 允许完成 | Exit 2: 拒绝，附反馈

set -euo pipefail

TEAMMATE="${CLAUDE_TEAMMATE_NAME:-}"

if [[ "$TEAMMATE" == "strategy" ]]; then
    V=$(ls -d Strategy/Script/v*/ 2>/dev/null | sort -V | tail -1)
    [ -z "$V" ] && { echo "FEEDBACK: Strategy/Script/ 下无版本目录"; exit 2; }
    for f in config.json risk-profile.json README.md; do
        [ ! -f "$V/$f" ] && { echo "FEEDBACK: 缺少 $V/$f"; exit 2; }
    done
    ls "$V"/strategy.{js,ts,py} &>/dev/null 2>&1 || { echo "FEEDBACK: 缺少策略主文件"; exit 2; }
    for field in max_position_size_pct stop_loss_pct max_drawdown_pct gas_budget_usd slippage_tolerance_pct; do
        grep -q "\"$field\"" "$V/risk-profile.json" || { echo "FEEDBACK: risk-profile.json 缺少字段: $field"; exit 2; }
    done
fi

if [[ "$TEAMMATE" == "backtest" ]]; then
    V=$(ls -d Strategy/Backtest/v*/ 2>/dev/null | sort -V | tail -1)
    [ -z "$V" ] && V=$(ls -d Strategy/Backtest/backtest_results/*/ 2>/dev/null | sort -V | tail -1)
    [ -z "$V" ] && { echo "FEEDBACK: 无回测输出目录"; exit 2; }
    # 至少有报告文件
    ls "$V"/*.{json,md} &>/dev/null 2>&1 || { echo "FEEDBACK: 回测目录缺少报告文件"; exit 2; }
fi

if [[ "$TEAMMATE" == "publish" ]]; then
    for S in Skills/*/; do
        [ "$S" = "Skills/okx-strategy-factory/" ] && continue
        [ "$S" = "Skills/templates/" ] && continue
        [ -d "$S" ] && [ ! -f "$S/manifest.json" ] && { echo "FEEDBACK: $S 缺少 manifest.json"; exit 2; }
    done
fi

exit 0
