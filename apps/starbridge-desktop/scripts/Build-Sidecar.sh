#!/bin/sh
set -eu

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset BASH_ENV ENV __PYVENV_LAUNCHER__ 2>/dev/null || true
unset DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH \
    DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH \
    LD_PRELOAD LD_LIBRARY_PATH 2>/dev/null || true

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
. "$SCRIPT_DIR/sidecar_environment.sh"

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    RUNNER=$REPO_ROOT/.venv/bin/python
else
    RUNNER=/usr/bin/python3
fi

sidecar_exec_clean "$RUNNER" "$SCRIPT_DIR/sidecar_builder.py" "$@"
