#!/bin/zsh
# Launch from Finder after uv sync --extra ui and model/device setup.
set -eu
cd "$(dirname "$0")/../.."
if [[ ! -x .venv/bin/python ]]; then
  echo 'Run uv sync --extra ui first / Ejecutá uv sync --extra ui primero.'
  read -r '?Press Enter / Enter para cerrar: '
  exit 1
fi
export HF_HOME="${HF_HOME:-$PWD/out/hf-cache}"
exec .venv/bin/python -m voztrad app "$@"
