#!/usr/bin/env bash
set -euo pipefail

# ── 策略部署脚本 ──────────────────────────────────────────────────────────────
# 用法:
#   ./deploy.sh <strategy> local        # 本地测试运行
#   ./deploy.sh <strategy> production    # 部署到 VPS
#   ./deploy.sh <strategy> status        # 查看运行状态
#   ./deploy.sh <strategy> stop          # 停止运行
#
# 示例:
#   ./deploy.sh grid-trading local
#   ./deploy.sh grid-trading production

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STRATEGY="${1:?用法: ./deploy.sh <strategy> <local|production|status|stop>}"
TARGET="${2:?用法: ./deploy.sh <strategy> <local|production|status|stop>}"

# ── 策略名到脚本的映射 ──────────────────────────────────────────────────────
declare -A STRATEGY_SCRIPTS=(
    ["grid-trading"]="Strategy/Script/eth_grid_v4.py"
)

SCRIPT_REL="${STRATEGY_SCRIPTS[$STRATEGY]:-}"
if [[ -z "$SCRIPT_REL" ]]; then
    echo "❌ 未知策略: $STRATEGY"
    echo "   可用策略: ${!STRATEGY_SCRIPTS[*]}"
    exit 1
fi

SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_REL"
SCRIPT_PARENT="$(dirname "$SCRIPT_PATH")"

# ── VPS 配置 ──────────────────────────────────────────────────────────────────
VPS_HOST="${VPS_HOST:?请设置 VPS_HOST 环境变量}"
VPS_USER="${VPS_USER:-ubuntu}"
VPS_SSH_KEY_ITEM="OpenClaw"
VPS_DEPLOY_DIR="/opt/strategy/$STRATEGY"

# ── 颜色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}▸${NC} $*"; }
warn()  { echo -e "${YELLOW}▸${NC} $*"; }
error() { echo -e "${RED}✖${NC} $*"; exit 1; }

# ══════════════════════════════════════════════════════════════════════════════
# LOCAL — 本地测试运行
# ══════════════════════════════════════════════════════════════════════════════
do_local() {
    info "本地运行 $STRATEGY (dry-run 模式)"

    # 检查脚本存在
    [[ -f "$SCRIPT_PATH" ]] || error "策略脚本不存在: $SCRIPT_PATH"

    # 检查 onchainos
    local onchainos_bin="$SCRIPT_DIR/Agentic Wallet/onchainos"
    if [[ ! -x "$onchainos_bin" ]]; then
        warn "本地 onchainos 不可执行，尝试 PATH 中查找..."
        command -v onchainos >/dev/null 2>&1 || error "onchainos 未找到"
    fi

    # 设置环境 + 运行
    info "ENV=local, DRY_RUN=true"
    info "按 Ctrl+C 停止"
    echo ""
    cd "$SCRIPT_PARENT"
    ENV=local python3 "$SCRIPT_PATH" tick
}

# ══════════════════════════════════════════════════════════════════════════════
# LOCAL RUN — 本地验证（N 个 tick，检查无报错）
# ══════════════════════════════════════════════════════════════════════════════
do_local_validate() {
    info "本地验证 $STRATEGY — 运行 3 个 tick 检查启动和连接"

    [[ -f "$SCRIPT_PATH" ]] || error "策略脚本不存在: $SCRIPT_PATH"

    cd "$SCRIPT_PARENT"

    # Tick 1: 检查启动 + RPC 连接 + 钱包余额
    info "Tick 1/3: 启动检查..."
    if ENV=local python3 "$SCRIPT_PATH" tick 2>&1; then
        info "Tick 1 ✓"
    else
        error "Tick 1 失败 — 检查日志"
    fi

    # Tick 2-3: 连续运行
    for i in 2 3; do
        info "Tick $i/3..."
        if ENV=local python3 "$SCRIPT_PATH" tick 2>&1; then
            info "Tick $i ✓"
        else
            error "Tick $i 失败"
        fi
    done

    echo ""
    info "✅ 本地验证通过 — 可以部署到生产环境"
    info "   运行: ./deploy.sh $STRATEGY production"
}

# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTION — 部署到 VPS
# ══════════════════════════════════════════════════════════════════════════════
do_production() {
    info "部署 $STRATEGY 到 VPS ($VPS_HOST)"

    # 1. 获取 SSH 密钥
    info "获取 SSH 密钥..."
    local ssh_key
    ssh_key=$(mktemp)
    trap "rm -f '$ssh_key'" EXIT
    op item get "$VPS_SSH_KEY_ITEM" --fields "private key" --vault AI > "$ssh_key" 2>/dev/null \
        || error "1Password 获取 SSH 密钥失败（确保已解锁）"
    chmod 600 "$ssh_key"

    local SSH="ssh -i $ssh_key -o StrictHostKeyChecking=no $VPS_USER@$VPS_HOST"
    local SCP="scp -i $ssh_key -o StrictHostKeyChecking=no"

    # 2. Pre-deploy 检查
    info "VPS 连通性检查..."
    $SSH "echo ok" >/dev/null 2>&1 || error "SSH 连接失败"

    info "VPS 环境检查..."
    $SSH "onchainos --version" >/dev/null 2>&1 || error "VPS 上 onchainos 不可用"

    local disk_avail
    disk_avail=$($SSH "df -BG / | tail -1 | awk '{print \$4}' | tr -d 'G'")
    [[ "$disk_avail" -gt 1 ]] || error "VPS 磁盘不足: ${disk_avail}GB"

    # 3. 备份当前版本
    info "备份当前版本..."
    $SSH "mkdir -p $VPS_DEPLOY_DIR && \
          if [ -d $VPS_DEPLOY_DIR/current ]; then \
            cp -r $VPS_DEPLOY_DIR/current $VPS_DEPLOY_DIR/backup-\$(date +%Y%m%d-%H%M%S); \
          fi"

    # 4. 上传新版本
    info "上传策略文件..."
    $SSH "mkdir -p $VPS_DEPLOY_DIR/staging"
    $SCP -r "$SCRIPT_PARENT/"*.py "$SCRIPT_PARENT/"*.json \
        "$VPS_USER@$VPS_HOST:$VPS_DEPLOY_DIR/staging/" 2>/dev/null

    # 上传生产环境配置（确保 ENV=production）
    $SCP "$SCRIPT_PARENT/env.production.json" \
        "$VPS_USER@$VPS_HOST:$VPS_DEPLOY_DIR/staging/" 2>/dev/null

    # 5. 切换版本
    info "激活新版本..."
    $SSH "cd $VPS_DEPLOY_DIR && \
          pm2 stop $STRATEGY-bot 2>/dev/null || true && \
          if [ -d current ]; then mv current current.old; fi && \
          mv staging current && \
          cd current && \
          ENV=production pm2 start eth_grid_v4.py --name $STRATEGY-bot --interpreter python3"

    # 6. 健康检查
    info "健康检查（等待 10s）..."
    sleep 10

    local pm2_status
    pm2_status=$($SSH "pm2 jlist" 2>/dev/null)
    if echo "$pm2_status" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for p in data:
    if p['name'] == '$STRATEGY-bot' and p['pm2_env']['status'] == 'online':
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
        info "✅ 进程运行中"
    else
        warn "进程未正常启动，回滚..."
        $SSH "cd $VPS_DEPLOY_DIR && pm2 stop $STRATEGY-bot 2>/dev/null || true && \
              rm -rf current && mv current.old current && \
              cd current && pm2 start eth_grid_v4.py --name $STRATEGY-bot --interpreter python3"
        error "部署失败，已回滚"
    fi

    # 检查启动后有无错误日志
    local errors
    errors=$($SSH "cd $VPS_DEPLOY_DIR/current && \
        if [ -f grid_bot_v4.log ]; then tail -20 grid_bot_v4.log | grep -i 'error\|exception\|traceback' || true; fi")
    if [[ -n "$errors" ]]; then
        warn "检测到错误日志:"
        echo "$errors"
        warn "请检查是否需要回滚: ./deploy.sh $STRATEGY stop"
    fi

    # 清理旧备份（保留最近 3 个）
    $SSH "cd $VPS_DEPLOY_DIR && ls -dt backup-* 2>/dev/null | tail -n +4 | xargs rm -rf 2>/dev/null || true"

    # 清理 current.old
    $SSH "rm -rf $VPS_DEPLOY_DIR/current.old"

    echo ""
    info "✅ $STRATEGY 部署成功"
    info "   查看状态: ./deploy.sh $STRATEGY status"
    info "   查看日志: ssh $VPS_USER@$VPS_HOST 'pm2 logs $STRATEGY-bot --lines 50'"
}

# ══════════════════════════════════════════════════════════════════════════════
# STATUS — 查看运行状态
# ══════════════════════════════════════════════════════════════════════════════
do_status() {
    if [[ "$TARGET" == "status" ]]; then
        info "查询 VPS 上 $STRATEGY 状态..."
        local ssh_key
        ssh_key=$(mktemp)
        trap "rm -f '$ssh_key'" EXIT
        op item get "$VPS_SSH_KEY_ITEM" --fields "private key" --vault AI > "$ssh_key" 2>/dev/null \
            || error "1Password 获取 SSH 密钥失败"
        chmod 600 "$ssh_key"
        ssh -i "$ssh_key" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" \
            "pm2 describe $STRATEGY-bot 2>/dev/null || echo '未找到进程'"
    fi
}

# ══════════════════════════════════════════════════════════════════════════════
# STOP — 停止运行
# ══════════════════════════════════════════════════════════════════════════════
do_stop() {
    info "停止 VPS 上的 $STRATEGY..."
    local ssh_key
    ssh_key=$(mktemp)
    trap "rm -f '$ssh_key'" EXIT
    op item get "$VPS_SSH_KEY_ITEM" --fields "private key" --vault AI > "$ssh_key" 2>/dev/null \
        || error "1Password 获取 SSH 密钥失败"
    chmod 600 "$ssh_key"
    ssh -i "$ssh_key" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" \
        "pm2 stop $STRATEGY-bot && pm2 delete $STRATEGY-bot"
    info "✅ 已停止"
}

# ══════════════════════════════════════════════════════════════════════════════
# 路由
# ══════════════════════════════════════════════════════════════════════════════
case "$TARGET" in
    local)       do_local ;;
    validate)    do_local_validate ;;
    production)  do_production ;;
    status)      do_status ;;
    stop)        do_stop ;;
    *)           error "未知目标: $TARGET (可选: local, validate, production, status, stop)" ;;
esac
