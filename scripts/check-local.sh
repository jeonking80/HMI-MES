#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
./scripts/check-python.sh all
(
    cd apps/web
    npm run test:screen
    npm run typecheck
    npm run lint
    npm run build
)
