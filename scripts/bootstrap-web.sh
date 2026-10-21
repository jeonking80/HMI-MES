#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
repo_root="$PWD"
cd apps/web
exec npm ci --cache "${npm_config_cache:-$repo_root/.cache/npm}"
