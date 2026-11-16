#!/usr/bin/env sh
# Starts the portfolio manager (Mac/Linux). Keep the terminal open while you work.
cd "$(dirname "$0")" || exit 1
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
$PY -c "import PIL" 2>/dev/null || { echo "Installing Pillow (one time only)..."; $PY -m pip install --upgrade pillow; }
exec $PY tools/manager-server.py
