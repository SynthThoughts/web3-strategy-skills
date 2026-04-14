#!/usr/bin/env bash
# lp-auto installer — symlink CLI into ~/.local/bin and create instances root.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
CLI_SRC="${SKILL_DIR}/cli.py"
CLI_LINK="${BIN_DIR}/lp-auto"

mkdir -p "${BIN_DIR}"
mkdir -p "${HOME}/.lp-auto/instances"

chmod +x "${CLI_SRC}"

if [[ -L "${CLI_LINK}" || -f "${CLI_LINK}" ]]; then
  echo "Removing existing ${CLI_LINK}"
  rm "${CLI_LINK}"
fi

ln -s "${CLI_SRC}" "${CLI_LINK}"
echo "✓ Installed lp-auto → ${CLI_LINK}"
echo "  Skill sources: ${SKILL_DIR}"
echo "  Instances root: ${HOME}/.lp-auto/instances"

# PATH check
if ! echo "${PATH}" | tr ':' '\n' | grep -qFx "${BIN_DIR}"; then
  echo
  echo "⚠ ${BIN_DIR} is not in your PATH. Add this to your shell rc:"
  echo "   export PATH=\"${BIN_DIR}:\$PATH\""
fi

echo
echo "Try: lp-auto --help"
