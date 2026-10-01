#!/usr/bin/env bash
# Run this plugin's tests the same way CI does.
# Usage: ./run_tests.sh [path-to-FiestaBoard-core]   (default: ../FiestaBoard)
set -euo pipefail

CORE="${1:-../FiestaBoard}"
if [ ! -d "$CORE/src/plugins" ]; then
  echo "FiestaBoard core not found at: $CORE" >&2
  echo "Pass the path to your FiestaBoard checkout: ./run_tests.sh /path/to/FiestaBoard" >&2
  exit 1
fi
CORE_ABS="$(cd "$CORE" && pwd)"
PLUGIN_ID=$(python3 -c "import json; print(json.load(open('manifest.json'))['id'])")

# Recreate the import scaffold CI builds (ignored by git).
mkdir -p plugins
touch plugins/__init__.py
[ -e "plugins/$PLUGIN_ID" ] || ln -s .. "plugins/$PLUGIN_ID"
[ -e "$PLUGIN_ID" ] || ln -s . "$PLUGIN_ID"

PYTHONPATH="$(pwd):$CORE_ABS" pytest tests/ -v \
  --cov=. --cov-report=term-missing --cov-fail-under=70 --ignore="$CORE_ABS"
