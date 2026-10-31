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
#  Keep this folder in your HOME folder, not on the Desktop or in
#  Documents/Downloads: macOS privacy protection blocks Terminal and
#  Python from those folders unless you grant access.
#
#  Keep the Terminal window that opens. It owns the tester's serial port.
#  Closing it, or pressing Ctrl-C in it, sends STOP to the tester first.
#
#  If macOS says it cannot open the file: right-click it, choose Open,
#  then Open again. That happens only for a ZIP download, not a git clone.
# =====================================================================

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/Python/ebc_framework"
VENV="$ROOT/.venv"
VPY="$VENV/bin/python"
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

pause_exit() {
  echo
  read -r -p "Press Enter to close this window. " _
  exit "${1:-1}"
}

privacy_help() {
  echo
  echo "macOS is blocking access to this folder:"
  echo "    $ROOT"
  echo "The Desktop, Documents and Downloads folders are protected by macOS"
  echo "privacy settings, so Terminal and Python may not read files there."
  echo
  echo "Fix: in Finder, drag the EBC-20-Tester folder into your home folder"
  echo "(the one with the house icon; Cmd+Shift+H opens it), then double-click"
  echo "start_ebc.command there."
  echo
  echo "Or allow it: System Settings > Privacy & Security > Files and Folders"
  echo "> Terminal > turn on the folder this is in."
  pause_exit 1
}

if [ ! -f "$APP/webui.py" ]; then
  echo "This file has to stay in the EBC-20-Tester folder,"
  echo "next to the Python/ebc_framework folder."
  pause_exit 1
fi

py_ok()      { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }
has_serial() { "$1" -c 'import serial' >/dev/null 2>&1; }
# Can this Python actually read the project files? Plain file tests from the
# shell are not enough: macOS may allow the shell and still block Python.
can_read()   { "$1" -c 'import sys; open(sys.argv[1], "rb").read(1)' "$ROOT/requirements.txt" >/dev/null 2>&1; }

PY=""
BASE=""

try() {
  [ -n "$PY" ] && return 0
  local c="$1"
  if [ "${c#/}" = "$c" ]; then              # a bare name: look it up
    c="$(command -v "$c" 2>/dev/null)" || return 0
  fi
  [ -x "$c" ] || return 0
  py_ok "$c" || return 0
  [ -z "$BASE" ] && BASE="$c"
  has_serial "$c" && PY="$c"
  return 0
}

# 1) the private environment from an earlier launch, if it is complete.
#    It is never used as the BASE for building a new one: if it is broken,
#    it gets repaired or rebuilt below from a real installed Python.
if [ -x "$VPY" ] && has_serial "$VPY"; then
  PY="$VPY"
fi

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
  if [ -x "$VPY" ] && py_ok "$VPY"; then
    # An earlier first start was interrupted: finish it in place.
    echo "Finishing the private Python environment in .venv"
    echo "(an earlier setup did not complete). This needs internet."
  else
    if [ -z "$BASE" ]; then
      echo "No Python 3.10 or newer was found on this Mac."
      echo "Install it from https://www.python.org/downloads/macos/"
      echo "(or with Homebrew: brew install python), then double-click"
      echo "this file again."
      pause_exit 1
    fi
    can_read "$BASE" || privacy_help
    [ -e "$VENV" ] && rm -rf "$VENV"        # a broken leftover: start over
    echo "First start: setting up a private Python environment in .venv"
    echo "using $BASE"
    echo "It installs pyserial and matplotlib once. This needs internet."
    echo
    "$BASE" -m venv "$VENV" || { echo "Could not create .venv."; pause_exit 1; }
  fi
  can_read "$VPY" || privacy_help
  "$VPY" -m pip install --upgrade pip >/dev/null 2>&1
  if ! "$VPY" -m pip install -r "$ROOT/requirements.txt"; then
    echo
    echo "Installing the requirements failed. Check the internet connection"
    echo "and double-click this file again; it picks up where it stopped."
    pause_exit 1
  fi
  PY="$VPY"
fi

can_read "$PY" || privacy_help
cd "$APP" || pause_exit 1
echo "Using $PY"
echo
"$PY" webui.py "$@"
status=$?
[ "$status" -ne 0 ] && pause_exit "$status"
exit 0
