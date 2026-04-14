#!/usr/bin/env sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/scripts/.venv"
if [ ! -f "$VENV/bin/python3" ]; then
    echo "Creating venv at $VENV..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -r "$SCRIPT_DIR/scripts/requirements.txt"
fi
exec "$VENV/bin/python3" "$SCRIPT_DIR/scripts/gup.py" "$@"
