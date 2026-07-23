#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    RUNNER=$REPO_ROOT/.venv/bin/python
else
    RUNNER=python3
fi

for VARIABLE in $(env | sed -n 's/^\(PYTHON[A-Za-z0-9_]*\)=.*/\1/p'); do
    unset "$VARIABLE"
done
unset __PYVENV_LAUNCHER__ 2>/dev/null || true

exec "$RUNNER" -I "$SCRIPT_DIR/sidecar_tester.py" "$@"
