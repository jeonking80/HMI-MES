#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v uv >/dev/null || { echo "Install uv before running this script." >&2; exit 127; }

# Use the repository environment even when another virtualenv is active.
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.cache/uv}"
export UV_PYTHON_DOWNLOADS=never
uv lock --check --offline
exec uv sync --frozen --python 3.12 --group dev
