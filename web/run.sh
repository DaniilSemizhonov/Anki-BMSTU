#!/bin/sh
set -eu
WEB_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$WEB_DIR/.venv/bin/python" ]; then
    python3 -m venv "$WEB_DIR/.venv"
fi
if ! "$WEB_DIR/.venv/bin/python" -c 'from importlib.metadata import version; assert version("anki") == "26.9.3"; assert version("markdownify") == "1.2.3"; assert version("PyYAML") == "6.0.3"' 2>/dev/null; then
    "$WEB_DIR/.venv/bin/python" -m pip install -r "$WEB_DIR/requirements.txt"
fi
exec "$WEB_DIR/.venv/bin/python" "$WEB_DIR/server.py" "$@"
