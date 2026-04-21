#!/usr/bin/env bash
# run.sh — Railway start command
# This script is the single entry point defined in railway.json.
# It runs from the project root so that `app` is resolvable as a package.

set -euo pipefail

echo "=============================="
echo "  S&P 500 Research Bot"
echo "  Python: $(python --version)"
echo "  PWD:    $(pwd)"
echo "=============================="

# Ensure the data directory exists (config.py also does this, but belt-and-braces)
mkdir -p "${DATA_DIR:-/tmp/spbot_data}"

# Run the bot as a module (so relative imports inside app/ work correctly)
exec python -m app.main
