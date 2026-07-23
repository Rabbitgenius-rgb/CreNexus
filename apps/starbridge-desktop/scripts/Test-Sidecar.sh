#!/bin/sh
set -eu

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    RUNNER=$REPO_ROOT/.venv/bin/python
else
    RUNNER=/usr/bin/python3
fi

for VARIABLE in $(/usr/bin/env | /usr/bin/sed -n \
    -e 's/^\(PYTHON[A-Za-z0-9_]*\)=.*/\1/p' \
    -e 's/^\(DYLD_[A-Za-z0-9_]*\)=.*/\1/p' \
    -e 's/^\(LD_[A-Za-z0-9_]*\)=.*/\1/p'); do
    unset "$VARIABLE"
done
unset __PYVENV_LAUNCHER__ 2>/dev/null || true
unset CODESIGN_ALLOCATE DEVELOPER_DIR MAGIC SDKROOT TOOLCHAINS XCRUN_CACHE_PATH \
    2>/dev/null || true

exec "$RUNNER" -I "$SCRIPT_DIR/sidecar_tester.py" "$@"
