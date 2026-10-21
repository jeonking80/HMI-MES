#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ ! -x .venv/bin/python ]]; then
    echo "Run ./scripts/bootstrap-python.sh first." >&2
    exit 127
fi

case "${1:-all}" in
    lint)
        .venv/bin/ruff check .
        exec .venv/bin/ruff format --check .
        ;;
    test)
        # pytest exit 5 (no collected tests) is deliberately preserved.
        exec .venv/bin/python -m pytest
        ;;
    all)
        .venv/bin/ruff check .
        .venv/bin/ruff format --check .
        exec .venv/bin/python -m pytest
        ;;
    *)
        echo "Usage: $0 [lint|test|all]" >&2
        exit 2
        ;;
esac
