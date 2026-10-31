#!/bin/bash
# =====================================================================
#  Double-click to start the EBC-A20 web interface (macOS).
#
#  Looks for a Python 3.10+ that already has pyserial. If none does, it
#  creates a private environment in .venv next to this file and installs
#  the requirements there once (needs internet that first time only).
#  A private environment also avoids Homebrew's Python refusing pip
#  installs ("externally-managed-environment").
#
#  Keep the Terminal window that opens. It owns the tester's serial port.
#  Closing it, or pressing Ctrl-C in it, sends STOP to the tester first.
#
#  If macOS says it cannot open the file: right-click it, choose Open,
#  then Open again. That happens only for a ZIP download, not a git clone.
# =====================================================================

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/Python/ebc_framework"
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

pause_exit() {
  echo
  read -r -p "Press Enter to close this window. " _
  exit "${1:-1}"
}

if [ ! -f "$APP/webui.py" ]; then
  echo "This file has to stay in the EBC-20-Tester folder,"
  echo "next to the Python/ebc_framework folder."
  pause_exit 1
fi

PY=""
BASE=""

try() {
  [ -n "$PY" ] && return 0
  local c="$1"
  if [ "${c#/}" = "$c" ]; then              # a bare name: look it up
    c="$(command -v "$c" 2>/dev/null)" || return 0
  fi
  [ -x "$c" ] || return 0
  "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
      >/dev/null 2>&1 || return 0
  [ -z "$BASE" ] && BASE="$c"
  "$c" -c 'import serial' >/dev/null 2>&1 && PY="$c"
  return 0
}

# 1) the private environment, if an earlier launch created it
try "$ROOT/.venv/bin/python"

# 2) an installed Python that already has pyserial. Explicit locations come
#    first: a double-clicked script does not read ~/.zshrc, so conda's PATH
#    setup is not in effect here. Apple's /usr/bin/python3 is 3.9 and is
#    left for last, since on a Mac without developer tools it pops up an
#    installer dialog.
try "$HOME/anaconda3/bin/python"
try "$HOME/miniconda3/bin/python"
try "$HOME/miniforge3/bin/python"
try "$HOME/opt/anaconda3/bin/python"
try "/opt/anaconda3/bin/python"
try "/opt/homebrew/bin/python3"
try "/usr/local/bin/python3"
try "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
try python3
try python

if [ -z "$PY" ]; then
  if [ -z "$BASE" ]; then
    echo "No Python 3.10 or newer was found on this Mac."
    echo "Install it from https://www.python.org/downloads/macos/"
    echo "(or with Homebrew: brew install python), then double-click"
    echo "this file again."
    pause_exit 1
  fi
  echo "First start: setting up a private Python environment in .venv"
  echo "It installs pyserial and matplotlib once. This needs internet."
  echo
  "$BASE" -m venv "$ROOT/.venv" || { echo "Could not create .venv."; pause_exit 1; }
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip >/dev/null 2>&1
  if ! "$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"; then
    echo
    echo "Installing the requirements failed. Check the internet connection,"
    echo "delete the .venv folder next to this file, and try again."
    pause_exit 1
  fi
  PY="$ROOT/.venv/bin/python"
fi

cd "$APP" || pause_exit 1
echo "Using $PY"
echo
"$PY" webui.py "$@"
status=$?
[ "$status" -ne 0 ] && pause_exit "$status"
exit 0
