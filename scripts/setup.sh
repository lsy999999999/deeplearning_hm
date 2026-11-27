#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
base=ab1607ae4fcfdf564d43389181e72f4f5ce651ee
patch_file="$PWD/patches/flashrsi-ca-rsi-ab1607a.patch"
if [ ! -d FlashRSI/.git ]; then
    git clone https://github.com/RUC-NLPIR/FlashRSI.git FlashRSI
    git -C FlashRSI checkout "$base"
fi
if [ "$(git -C FlashRSI rev-parse HEAD)" != "$base" ]; then
    echo 'FlashRSI HEAD differs from the pinned base; preserving the existing checkout.' >&2
    exit 1
fi
if git -C FlashRSI apply --check "$patch_file" 2>/dev/null; then
    git -C FlashRSI apply "$patch_file"
elif ! git -C FlashRSI apply --reverse --check "$patch_file" 2>/dev/null; then
    echo 'Existing checkout differs from the patch; inspect it before setup.' >&2
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
