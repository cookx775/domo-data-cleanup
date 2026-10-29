#!/bin/sh
# Starts Domo Cleanup with the .venv that "Install Domo Cleanup.command" created, then falls
# back to python3 or python with the packages installed.
cd "$(dirname "$0")/app" || exit 1
# An absolute path: Python 3.15 warns about sys.prefix when started through "..".
VENV_PYTHON="$(cd .. && pwd)/.venv/bin/python"
for candidate in "$VENV_PYTHON" python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys, requests, yaml, jsonschema; sys.exit(not sys.version_info[:2] >= (3, 10))' >/dev/null 2>&1; then
    exec "$candidate" cleanup.py open
  fi
done
echo 'Domo Cleanup is not installed for any Python 3.10 or newer found here.'
echo 'Double-click "Install Domo Cleanup.command" first, then open this file again.'
exit 1
