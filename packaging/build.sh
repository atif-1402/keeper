#!/usr/bin/env bash
# Build a standalone Linux binary at dist/keeper.
# Run from the repo root:  ./packaging/build.sh
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --with pyinstaller pyinstaller \
    --onefile \
    --name keeper \
    --paths . \
    --collect-submodules pypdf \
    --collect-submodules docx \
    packaging/entry.py

echo
echo "Binary: $(pwd)/dist/keeper"
./dist/keeper --version
