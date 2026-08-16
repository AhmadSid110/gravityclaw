#!/usr/bin/env bash
set -euo pipefail

# Canonical single-user installer. It installs the already-reviewed source
# artifact into a dedicated virtual environment, then delegates initialization
# to the same `gravityclaw setup` path used by automation.
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${GRAVITYCLAW_INSTALL_DIR:-${HOME}/.local/lib/gravityclaw}"
PYTHON="${PYTHON:-python3}"

"${PYTHON}" -c 'import sys; sys.exit("Python 3.12+ required") if sys.version_info < (3, 12) else None'
mkdir -p "${INSTALL_ROOT}"
"${PYTHON}" -m venv "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/python" -m pip install --upgrade pip
"${INSTALL_ROOT}/venv/bin/python" -m pip install "${SOURCE_DIR}"
"${INSTALL_ROOT}/venv/bin/gravityclaw" setup
echo "GravityClaw installed. Authenticate AGY through its official flow, then run:"
echo "  ${INSTALL_ROOT}/venv/bin/gravityclaw doctor"
