#!/usr/bin/env bash
# TeammateIdle hook — 空闲时检查待办
# Exit 0: 允许空闲 | Exit 2: 分配工作

set -euo pipefail

TEAMMATE="${CLAUDE_TEAMMATE_NAME:-}"

case "$TEAMMATE" in
    backtest)
        for v in Strategy/Script/v*/; do
            ver=$(basename "$v")
            [ ! -d "Strategy/Backtest/$ver" ] && [ -f "$v/risk-profile.json" ] && {
                echo "FEEDBACK: $ver 未回测。请验证 $v"; exit 2; }
        done ;;
esac

exit 0
