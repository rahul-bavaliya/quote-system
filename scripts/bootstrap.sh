#!/usr/bin/env bash
# Create .venv with its own pip and install the project into it.
# Works on the project dev container, on a Codespaces recovery container (system Python without pip)
# and on a plain Linux/macOS machine. Usage: scripts/bootstrap.sh   (or: make install)
set -euo pipefail
cd "$(dirname "$0")/.."

# Only one setup at a time. A Codespace runs this on start-up in the background; `make run` / `make install`
# typed meanwhile would start a second pip into the same .venv and the two collide. Wait for the first instead.
if command -v flock >/dev/null 2>&1; then
  exec 9>.bootstrap.lock
  if ! flock -n 9; then
    echo "Setup is already running (the Codespace starts it automatically) - waiting for it to finish..."
    flock 9
    if [ -x .venv/bin/python ] && .venv/bin/python scripts/check_deps.py >/dev/null 2>&1; then
      echo "Ready: set up by the other run."
      exit 0
    fi
  fi
fi

pick_python() {
  for p in python3.12 python3.13 python3.11 /usr/local/bin/python3 python3 python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  BASE=$(pick_python) || {
    echo "No Python 3.11+ found. In Codespaces run 'Codespaces: Rebuild Container' (F1), or install Python 3.12." >&2
    exit 1
  }
  echo "Creating .venv with $BASE ($("$BASE" --version))"
  rm -rf .venv
  if ! "$BASE" -m venv .venv 2>/dev/null; then
    # Debian/Ubuntu without python3-venv: make the venv without pip, then bootstrap pip into it
    "$BASE" -m venv --without-pip .venv
    if curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py 2>/dev/null; then
      .venv/bin/python /tmp/get-pip.py -q
    else
      # second route: the pip wheel straight from PyPI (a wheel can run itself to install pip)
      WHEEL_URL=$(curl -fsSL https://pypi.org/pypi/pip/json | .venv/bin/python -c \
        'import json,sys; print([u["url"] for u in json.load(sys.stdin)["urls"] if u["filename"].endswith(".whl")][0])')
      WHEEL="/tmp/$(basename "$WHEEL_URL")"   # pip insists on the real wheel file name
      curl -fsSL "$WHEEL_URL" -o "$WHEEL"
      .venv/bin/python "$WHEEL/pip" install -q --no-index "$WHEEL"
    fi
  fi
fi

echo "Installing dependencies into .venv - first time takes 2-5 minutes (about 700 MB), progress below."
.venv/bin/python -m pip install -q --upgrade pip
# no -q here: show each package so a long download does not look like a hang
set +e
.venv/bin/python -m pip install -r requirements.lock 2>&1 | grep -Ev "^(Requirement already satisfied|  Using cached)"
rc=${PIPESTATUS[0]}
set -e
[ "$rc" -eq 0 ] || { echo "Dependency install failed - see the messages above." >&2; exit 1; }
.venv/bin/python -m pip install -q --no-deps -e .
.venv/bin/python -m pip install -q ruff
echo "Ready: $(.venv/bin/python --version) in .venv  (make pipeline / make run use it automatically)"
