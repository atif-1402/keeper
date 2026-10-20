#!/usr/bin/env bash
# Keeper installer for Linux and macOS.
#
#     curl -fsSL https://raw.githubusercontent.com/atif-1402/keeper/main/install.sh | bash
#
# Downloads the prebuilt binary for your platform into ~/.local/bin.
# No sudo, no system files, fully idempotent — re-run it to update.
# (Windows: see the README — `pipx install git+https://github.com/atif-1402/keeper`)
set -euo pipefail

REPO="atif-1402/keeper"
BIN_DIR="${KEEPER_BIN_DIR:-$HOME/.local/bin}"
# Overridable for mirrors and tests.
RELEASE_BASE="${KEEPER_RELEASE_BASE:-https://github.com/$REPO/releases/latest/download}"

say()  { printf '  %s\n' "$*"; }
fail() { printf 'keeper install: %s\n' "$*" >&2; exit 1; }

os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m)
case "$os" in
  linux)  os=linux ;;
  darwin) os=macos ;;
  *) fail "unsupported OS '$os' — on Windows use: pipx install git+https://github.com/$REPO" ;;
esac
case "$arch" in
  x86_64|amd64)  arch=x86_64 ;;
  aarch64|arm64) arch=aarch64 ;;
  *) fail "unsupported architecture '$arch'" ;;
esac

asset="keeper-$os-$arch"
url="$RELEASE_BASE/$asset"
dest="$BIN_DIR/keeper"

say "installing $asset -> $dest"
mkdir -p "$BIN_DIR"
if curl -fSL --retry 3 -o "$dest.tmp" "$url"; then
  chmod +x "$dest.tmp"
  mv "$dest.tmp" "$dest"
else
  # Download failed (asset missing or no network): fall back to Python.
  rm -f "$dest.tmp"
  say "could not fetch the $asset binary — installing from source instead"
  if command -v pipx >/dev/null 2>&1; then
    pipx install --force "git+https://github.com/$REPO.git"
  elif command -v python3 >/dev/null 2>&1 \
       && python3 -m pip install --user --upgrade "git+https://github.com/$REPO.git"; then
    say "installed via pip --user"
  else
    fail "need pipx (recommended) or a Python 3.11+ pip — install one and re-run"
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    say "NOTE: $BIN_DIR is not on your PATH — add it to your shell rc:"
    say '  echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> ~/.bashrc && source ~/.bashrc'
    ;;
esac

if command -v ollama >/dev/null 2>&1; then
  say "Ollama found — model verdicts ready (run 'ollama pull gemma3:1b' if needed)"
else
  say "tip: get Ollama (https://ollama.com) + 'ollama pull gemma3:1b' for AI verdicts;"
  say "     without it keeper still works with its rule engine"
fi
say "try:  keeper ~/Downloads"
