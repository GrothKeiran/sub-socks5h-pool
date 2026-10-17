#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .cache/tmp data/tmp
export TMPDIR="$PWD/.cache/tmp"
export XDG_CACHE_HOME="$PWD/.cache"
export PYTHONPYCACHEPREFIX="$PWD/.cache/pycache"
exec python3 -m subpool
