#!/bin/bash
# Grid Trading v4 Skill Installer
# Supports: Claude Code, Cursor, Gemini CLI, OpenClaw
#
# Usage:
#   ./install.sh                          # Auto-detect platform, install to project
#   ./install.sh --platform claude        # Specify platform explicitly
#   ./install.sh --global                 # Install globally (user home)
#   ./install.sh --platform openclaw      # Install to OpenClaw skills directory
#   ./install.sh --platform gemini --global
#
# Platforms:
#   claude   -> .claude/skills/grid-trading/
#   cursor   -> .cursor/skills/grid-trading/
#   gemini   -> .gemini/skills/grid-trading/
#   openclaw -> ~/.openclaw/skills/grid-trading/ (always global, symlink)

set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# --- Defaults ---
PLATFORM=""
GLOBAL=false
SKILL_SRC="$(cd "$(dirname "$0")" && pwd)"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case $1 in
        --platform)
            PLATFORM="$2"
            shift 2
            ;;
        --global)
            GLOBAL=true
            shift
            ;;
        -h|--help)
            echo "Grid Trading v4 Skill Installer"
            echo ""
            echo "Usage: $0 [--platform claude|cursor|gemini|openclaw] [--global]"
            echo ""
            echo "Options:"
            echo "  --platform NAME   Specify target platform (claude, cursor, gemini, openclaw)"
            echo "  --global          Install to user home directory instead of current project"
            echo ""
            echo "Auto-detection (when --platform is omitted):"
            echo "  Checks for .claude/, .cursor/, .gemini/, .openclaw/ in current directory"
            echo "  If multiple found, installs to all detected platforms"
            echo ""
            echo "OpenClaw:"
            echo "  Always installs globally to ~/.openclaw/skills/grid-trading/"
            echo "  Optionally registers cron jobs for tick (5min) and daily report"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
done

# --- Validate platform ---
if [[ -n "$PLATFORM" ]]; then
    case "$PLATFORM" in
        claude|cursor|gemini|openclaw) ;;
        *)
            echo -e "${RED}Invalid platform: $PLATFORM${NC}" >&2
            echo "Valid platforms: claude, cursor, gemini, openclaw"
            exit 1
            ;;
    esac
fi

# --- Detect platforms ---
detect_platforms() {
    local platforms=()
    local base_dir="$1"

    if [[ -d "$base_dir/.claude" ]]; then
        platforms+=("claude")
    fi
    if [[ -d "$base_dir/.cursor" ]]; then
        platforms+=("cursor")
    fi
    if [[ -d "$base_dir/.gemini" ]]; then
        platforms+=("gemini")
    fi
    if [[ -d "$HOME/.openclaw" ]]; then
        platforms+=("openclaw")
    fi

    echo "${platforms[@]}"
}

# --- Install function ---
install_skill() {
    local target_dir="$1"
    local platform="$2"

    echo -e "${BLUE}Installing to ${platform}...${NC}"

    # Create target directory
    mkdir -p "$target_dir"

    # Copy SKILL.md
    cp "$SKILL_SRC/SKILL.md" "$target_dir/SKILL.md"
    echo -e "  ${GREEN}+${NC} SKILL.md"

    # Copy references/
    if [[ -d "$SKILL_SRC/references" ]]; then
        mkdir -p "$target_dir/references"
        cp "$SKILL_SRC/references/"*.md "$target_dir/references/" 2>/dev/null || true
        local ref_count
        ref_count=$(ls -1 "$target_dir/references/"*.md 2>/dev/null | wc -l | tr -d ' ')
        echo -e "  ${GREEN}+${NC} references/ (${ref_count} files)"
    fi

    echo -e "  ${GREEN}Installed to:${NC} $target_dir"

    # OpenClaw post-install: register cron jobs
    if [[ "$platform" == "openclaw" ]]; then
        openclaw_post_install "$target_dir"
    fi
}

# --- OpenClaw post-install ---
openclaw_post_install() {
    local target_dir="$1"
    local script_dir="$HOME/.openclaw/scripts"

    echo ""
    echo -e "${BLUE}OpenClaw post-install...${NC}"

    # Copy strategy script to OpenClaw scripts directory
    if [[ -f "$SKILL_SRC/references/eth_grid_v4.py" ]]; then
        mkdir -p "$script_dir"
        cp "$SKILL_SRC/references/eth_grid_v4.py" "$script_dir/eth_grid_v4.py"
        echo -e "  ${GREEN}+${NC} scripts/eth_grid_v4.py"
    fi

    # Check if openclaw cron is available
    if command -v openclaw &>/dev/null; then
        echo ""
        echo -e "${YELLOW}Register cron jobs? (requires running gateway)${NC}"
        echo "  1) eth-grid-tick:  every 5 min  → eth_grid_v4.py tick"
        echo "  2) eth-grid-daily: daily 08:00  → eth_grid_v4.py report"
        echo ""
        read -rp "Register cron jobs via OpenClaw? [y/N] " answer
        if [[ "${answer,,}" == "y" ]]; then
            echo -e "  ${BLUE}Adding cron jobs...${NC}"
            openclaw cron add \
                --name "eth-grid-tick" \
                --schedule "*/5 * * * *" \
                --command "cd $script_dir && python3 eth_grid_v4.py tick" \
                2>/dev/null && echo -e "  ${GREEN}+${NC} eth-grid-tick (every 5 min)" \
                || echo -e "  ${YELLOW}!${NC} eth-grid-tick: skipped (gateway not running or already exists)"

            openclaw cron add \
                --name "eth-grid-daily" \
                --schedule "0 0 * * *" \
                --command "cd $script_dir && python3 eth_grid_v4.py report" \
                2>/dev/null && echo -e "  ${GREEN}+${NC} eth-grid-daily (daily 08:00 CST)" \
                || echo -e "  ${YELLOW}!${NC} eth-grid-daily: skipped (gateway not running or already exists)"
        else
            echo -e "  Skipped. You can add manually:"
            echo "    openclaw cron add --name eth-grid-tick --schedule '*/5 * * * *' --command 'cd $script_dir && python3 eth_grid_v4.py tick'"
            echo "    openclaw cron add --name eth-grid-daily --schedule '0 0 * * *' --command 'cd $script_dir && python3 eth_grid_v4.py report'"
        fi
    else
        echo -e "  ${YELLOW}openclaw CLI not found. Install: npm i -g @anthropic-ai/openclaw${NC}"
        echo "  After installing, add cron jobs manually:"
        echo "    openclaw cron add --name eth-grid-tick --schedule '*/5 * * * *' --command 'cd $script_dir && python3 eth_grid_v4.py tick'"
    fi

    # Verify skill is visible
    if command -v openclaw &>/dev/null; then
        echo ""
        if openclaw skills list 2>/dev/null | grep -q "grid-trading"; then
            echo -e "  ${GREEN}OK${NC} openclaw skills list shows grid-trading"
        else
            echo -e "  ${YELLOW}!${NC} grid-trading not yet visible in 'openclaw skills list'"
            echo "    This is normal if you just installed. Restart the gateway to pick it up."
        fi
    fi
}

# --- Resolve target directories ---
get_target_dir() {
    local platform="$1"
    local base_dir

    if [[ "$GLOBAL" == true ]]; then
        base_dir="$HOME"
    else
        base_dir="$(pwd)"
    fi

    case "$platform" in
        claude) echo "$base_dir/.claude/skills/grid-trading" ;;
        cursor) echo "$base_dir/.cursor/skills/grid-trading" ;;
        gemini) echo "$base_dir/.gemini/skills/grid-trading" ;;
        openclaw) echo "$HOME/.openclaw/skills/grid-trading" ;;
    esac
}

# --- Main ---
echo -e "${BLUE}Grid Trading v4 Skill Installer${NC}"
echo ""

INSTALLED=0

if [[ -n "$PLATFORM" ]]; then
    # Explicit platform
    target=$(get_target_dir "$PLATFORM")
    install_skill "$target" "$PLATFORM"
    INSTALLED=1
else
    # Auto-detect
    if [[ "$GLOBAL" == true ]]; then
        detect_dir="$HOME"
    else
        detect_dir="$(pwd)"
    fi

    platforms=$(detect_platforms "$detect_dir")

    if [[ -z "$platforms" ]]; then
        echo -e "${YELLOW}No AI agent platform detected in ${detect_dir}${NC}"
        echo ""
        echo "To auto-detect, run this from a project directory that has one of:"
        echo "  .claude/   .cursor/   .gemini/"
        echo ""
        echo "Or specify a platform explicitly:"
        echo "  $0 --platform claude"
        echo "  $0 --platform cursor"
        echo "  $0 --platform gemini"
        echo ""
        echo "Or install globally:"
        echo "  $0 --platform claude --global"
        exit 1
    fi

    for p in $platforms; do
        target=$(get_target_dir "$p")
        install_skill "$target" "$p"
        INSTALLED=$((INSTALLED + 1))
        echo ""
    done
fi

# --- Verify ---
echo -e "${BLUE}--- Verification ---${NC}"

if [[ -n "$PLATFORM" ]]; then
    check_platforms="$PLATFORM"
else
    check_platforms="$platforms"
fi

ALL_OK=true
for p in $check_platforms; do
    target=$(get_target_dir "$p")
    if [[ -f "$target/SKILL.md" ]]; then
        echo -e "  ${GREEN}OK${NC} $p: SKILL.md found"
    else
        echo -e "  ${RED}FAIL${NC} $p: SKILL.md missing"
        ALL_OK=false
    fi
done

echo ""
if [[ "$ALL_OK" == true ]]; then
    echo -e "${GREEN}Installation complete.${NC} Installed to ${INSTALLED} platform(s)."
    echo ""
    echo "Quick start:"
    echo "  Ask your AI agent: \"Use the grid-trading skill to create a grid bot for ETH/USDC on Base\""
else
    echo -e "${RED}Installation had errors. Please check the output above.${NC}"
    exit 1
fi
