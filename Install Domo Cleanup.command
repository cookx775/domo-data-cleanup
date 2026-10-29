#!/bin/sh
# Installs Domo Cleanup into a private Python environment (.venv) next to app/.
cd "$(dirname "$0")" || exit 1
STEP="Checking the app folder"

fail() {
  echo
  echo "Install stopped at: $STEP"
  for line in "$@"; do echo "$line"; done
  exit 1
}

step() {
  STEP="$2"
  echo
  echo "[$1/5] $STEP"
}

supported() {
  "$@" -c 'import sys; sys.exit(not sys.version_info[:2] >= (3, 10))' >/dev/null 2>&1
}

[ -f app/cleanup.py ] || fail "Cannot find the app folder next to this file." \
  "Extract Domo-Cleanup.zip fully and open this file from the extracted domo-cleanup folder."
echo "Installing Domo Cleanup in $(pwd)"

step 1 "Finding Python 3.10 or newer"
PY=""
for candidate in python3 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
  if command -v "$candidate" >/dev/null 2>&1 && supported "$candidate"; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || fail "Python 3.10 or newer was not found." \
  "Install Python from python.org, then double-click this file again."
"$PY" -c 'import sys; print("Using Python " + sys.version.split()[0] + " at " + sys.executable)'

step 2 "Creating the private Python environment (.venv)"
VPY=.venv/bin/python
if ! { [ -x "$VPY" ] && supported "$VPY"; }; then
  "$PY" -m venv --clear .venv || fail "Python could not create the .venv folder here."
fi
"$VPY" -c 'import sys; print("Environment Python " + sys.version.split()[0])' || fail

step 3 "Upgrading pip"
"$VPY" -m pip install --upgrade pip || fail "Check internet access to pypi.org (ask IT whether a proxy is needed)."

step 4 "Installing Domo Cleanup and its libraries"
"$VPY" -m pip install --prefer-binary --find-links ./app/wheels -e ./app \
  || fail "Check internet access to pypi.org (ask IT whether a proxy is needed)."

step 5 "Checking the install"
"$VPY" -c 'import requests, yaml, jsonschema, collector, shared; print("install ok")' || fail

echo
echo 'Domo Cleanup is installed. Double-click "Open Domo Cleanup.command" to start it.'
