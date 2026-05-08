#!/usr/bin/env bash
# Set up a Python virtual environment for the Exercise Planner.
# Usage: bash setup_venv.sh
set -euo pipefail

cd "$(dirname "$0")"

# Pick a Python interpreter (prefer 3.12 / 3.11 / 3.10, else python3).
PYTHON_BIN="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)"
echo "Using interpreter: $PYTHON_BIN ($($PYTHON_BIN --version))"

# Create venv if missing OR broken (e.g. carried over from another OS).
if [ ! -x ".venv/bin/python" ] || [ ! -f ".venv/bin/activate" ]; then
  echo "Creating fresh .venv ..."
  rm -rf .venv
  "$PYTHON_BIN" -m venv .venv
else
  echo ".venv already exists — reusing."
fi

# Activate and install.
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# Make sure a .env exists.  Copy from .env.example on first run.
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "Created .env from .env.example — fill in your GEMINI_API_KEY before running live_demo.py."
fi

echo ""
echo "Done. To use the venv:"
echo "  source .venv/bin/activate"
echo "  python live_demo.py                           # full Gemini-driven loop + Prefab dashboard"
echo "  python exercise_planner_server.py             # raw MCP server (stdio)"
echo "  fastmcp dev apps exercise_planner_server.py   # browser preview of Prefab UI"
