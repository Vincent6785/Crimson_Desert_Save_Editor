#!/usr/bin/env bash
# Start the editor (uses .venv if present, otherwise the system python3).
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" cd_items_gui.py "$@"
