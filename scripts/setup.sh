#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f FlashRSI/pyproject.toml ]; then
    echo 'FlashRSI source is missing; restore FlashRSI/ from this repository.' >&2
    exit 1
fi
if command -v uv >/dev/null 2>&1; then
    uv_bin=uv
else
    if [ ! -x .tools/bin/uv ]; then
        python3 -m pip install --target .tools uv==0.13.10
    fi
    uv_bin="$PWD/.tools/bin/uv"
fi
if [ ! -x .venv/bin/python ]; then
    "$uv_bin" venv --python 3.12 .venv
fi
"$uv_bin" pip install --python .venv/bin/python -r requirements-lock.txt
"$uv_bin" pip install --python .venv/bin/python --no-deps -e ./FlashRSI
