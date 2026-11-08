#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${BOND_SCREEN_PYTHON:-python3}"
ENV_DIR="${BOND_SCREEN_VENV:-$ROOT/.venv}"
if [[ ! -x "$ENV_DIR/bin/python" ]]; then
  "$PYTHON" -m venv "$ENV_DIR"
fi
if [[ ! -f "$ENV_DIR/.bond-screen-requirements" ]] || ! cmp -s "$ROOT/requirements.txt" "$ENV_DIR/.bond-screen-requirements"; then
  "$ENV_DIR/bin/python" -m pip install -r "$ROOT/requirements.txt"
  cp "$ROOT/requirements.txt" "$ENV_DIR/.bond-screen-requirements"
fi
exec "$ENV_DIR/bin/python" "$ROOT/scripts/run_market.py" "$@"
