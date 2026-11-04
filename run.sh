#!/usr/bin/env bash
# Start VoiceGetter on Linux or macOS. The first run installs everything.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install -U pip
  pip install -r requirements.txt
else
  . .venv/bin/activate
fi
python -m app.main
